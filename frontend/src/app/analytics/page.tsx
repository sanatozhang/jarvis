"use client";

import { useT } from "@/lib/i18n";
import { CountUp } from "@/components/CountUp";
import { Suspense, useEffect, useState, useCallback, useMemo } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  fetchAnalyticsDashboard, fetchRuleAccuracy, fetchProblemTypeStats, fetchClassificationStats,
  backfillClassifications, fetchIssueDetail, formatLocalTime, fetchVocClassificationStats,
  fetchVocTrend, fetchVocMovers, fetchVocWeeklyDigest, generateVocWeeklyDigest, fetchFixEffectiveness,
  type AnalyticsDashboard, type FailReasonItem, type RuleAccuracyStat, type ProblemTypeStats, type ClassificationStats,
  type LocalIssueItem, type VocClassificationStats, type VocTrend, type VocMoversResponse, type VocWeeklyDigest,
  type FixEffectiveness,
} from "@/lib/api";
import {
  thisMonday, lastMonday, isMondayISO, thisMonthStart, lastMonthStart, isMonthStartISO,
  resolveRange, alignedPrevPeriod, type TimeRange,
} from "@/lib/timeRange";

const S = {
  surface: "var(--j-surface)", overlay: "var(--j-panel)", hover: "var(--j-hover)",
  border: "var(--j-border)", accent: "var(--j-accent)", accentBg: "var(--j-accent-soft)",
  text1: "var(--j-ink)", text2: "var(--j-graphite)", text3: "var(--j-faint)",
};

// Token 大数千分位
function fmtTokens(n: number): string {
  return Math.round(n || 0).toLocaleString("en-US");
}
// 费用：保留 2-4 位小数（小额多保留几位）
function fmtCost(n: number): string {
  const v = n || 0;
  if (v === 0) return "0.00";
  if (v < 0.01) return v.toFixed(4);
  if (v < 1) return v.toFixed(3);
  return v.toFixed(2);
}

function StatCard({ label, value, sub, color, index = 0 }: { label: string; value: string | number; sub?: string; color?: string; index?: number }) {
  const numeric = typeof value === "number" && Number.isFinite(value);
  return (
    <div className="rounded-xl px-4 py-4 j-card j-rise" style={{ background: S.surface, border: `1px solid ${S.border}`, ["--d" as string]: `${index * 0.06}s` }}>
      <p className="text-xs" style={{ color: S.text3 }}>{label}</p>
      <p className="mt-1 text-2xl font-bold tabular-nums j-digits" style={{ color: color || S.text1 }}>
        {numeric ? <CountUp value={value} /> : value}
      </p>
      {sub && <p className="mt-0.5 text-[11px] font-mono" style={{ color: S.text3 }}>{sub}</p>}
    </div>
  );
}

// 深链：?week=YYYY-MM-DD（自然周，须为周一）或 ?month=YYYY-MM-01（自然月）或
// ?days=N；三者互斥，缺省 = 上周。坏值（非周一/非月初/非法数字）静默降级到
// 默认值，绝不 throw —— 一个错的分享链接不该让页面挂掉。
function parseRange(sp: URLSearchParams): TimeRange {
  const wk = sp.get("week");
  if (wk && isMondayISO(wk)) return { kind: "week", weekStart: wk };
  const mo = sp.get("month");
  if (mo && isMonthStartISO(mo)) return { kind: "month", monthStart: mo };
  const n = parseInt(sp.get("days") || "", 10);
  if (Number.isFinite(n) && n >= 1 && n <= 3650) return { kind: "days", days: n };
  return { kind: "week", weekStart: lastMonday() };
}

function AnalyticsPageInner() {
  const t = useT();
  const router = useRouter();
  const searchParams = useSearchParams();

  const range = useMemo(() => parseRange(searchParams), [searchParams]);
  const rangeKey = range.kind === "week" ? `week:${range.weekStart}`
    : range.kind === "month" ? `month:${range.monthStart}`
    : `days:${range.days}`;
  const resolved = useMemo(() => resolveRange(range), [rangeKey]);

  const updateQuery = useCallback((patch: { week?: string; month?: string; days?: number }) => {
    const next = new URLSearchParams(searchParams.toString());
    if (patch.week !== undefined) {
      next.delete("days"); next.delete("month");
      if (patch.week === lastMonday()) next.delete("week");
      else next.set("week", patch.week);
    }
    if (patch.month !== undefined) {
      next.delete("days"); next.delete("week");
      next.set("month", patch.month);
    }
    if (patch.days !== undefined) {
      next.delete("week"); next.delete("month");
      next.set("days", String(patch.days));
    }
    const qs = next.toString();
    router.replace(qs ? `?${qs}` : "?", { scroll: false });
  }, [router, searchParams]);

  const PRESETS = useMemo(() => [
    { key: "cur", range: { kind: "week", weekStart: thisMonday() } as TimeRange, label: t("本周（进行中）") },
    { key: "last", range: { kind: "week", weekStart: lastMonday() } as TimeRange, label: t("上周") },
    { key: "monthCur", range: { kind: "month", monthStart: thisMonthStart() } as TimeRange, label: t("本月至今") },
    { key: "monthLast", range: { kind: "month", monthStart: lastMonthStart() } as TimeRange, label: t("上个月") },
    { key: "m3", range: { kind: "days", days: 90 } as TimeRange, label: t("近 3 个月") },
    { key: "m6", range: { kind: "days", days: 180 } as TimeRange, label: t("近 6 个月") },
    { key: "y1", range: { kind: "days", days: 365 } as TimeRange, label: t("近 1 年") },
  ], [t]);

  const isPresetActive = (p: TimeRange) => {
    if (p.kind !== range.kind) return false;
    if (p.kind === "week") return p.weekStart === (range as { weekStart: string }).weekStart;
    if (p.kind === "month") return p.monthStart === (range as { monthStart: string }).monthStart;
    return p.days === (range as { days: number }).days;
  };

  const [data, setData] = useState<AnalyticsDashboard | null>(null);
  const [customDays, setCustomDays] = useState("");
  const [loading, setLoading] = useState(true);
  const [ruleAccuracy, setRuleAccuracy] = useState<RuleAccuracyStat[]>([]);
  const [ptStats, setPtStats] = useState<ProblemTypeStats | null>(null);
  const [clsStats, setClsStats] = useState<ClassificationStats | null>(null);
  const [vocStats, setVocStats] = useState<VocClassificationStats | null>(null);
  const [vocTrend, setVocTrend] = useState<VocTrend | null>(null);
  const [vocMovers, setVocMovers] = useState<VocMoversResponse | null>(null);
  const [fixEffectiveness, setFixEffectiveness] = useState<FixEffectiveness | null>(null);
  const [digest, setDigest] = useState<VocWeeklyDigest | null>(null);
  const [digestLoading, setDigestLoading] = useState(false);
  const [digestRegenerating, setDigestRegenerating] = useState(false);
  const [digestError, setDigestError] = useState("");
  const [taxonomyMode, setTaxonomyMode] = useState<"voc" | "legacy">("voc");
  const [expandedVocGroup, setExpandedVocGroup] = useState<string | null>(null);
  const [expandedCat, setExpandedCat] = useState<string | null>(null);
  const [selectedDevice, setSelectedDevice] = useState<string>("all");
  const [backfilling, setBackfilling] = useState(false);
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

  const load = useCallback(async () => {
    setLoading(true);
    const w = { dateFrom: resolved.dateFrom, dateTo: resolved.dateTo };
    // 「周」「月」两种自然档位都需要显式对齐基线（同跨度同起点），而不是
    // 后端默认推导的「紧邻前一段」——本周三看「本周一~周三」时，紧邻前段是
    // 「上周五~周日」，工作日 vs 周末不可比；月份长度不一，紧邻前段天数也
    // 会跟当月错位。其它窗口（3/6/12 月、自定义天数）用后端默认推导即可。
    const prev = (range.kind === "week" || range.kind === "month") ? alignedPrevPeriod(resolved) ?? undefined : undefined;
    try {
      const [dash, ra, pt, cls, voc, vocTrendRes, vocMoversRes, fixEff] = await Promise.all([
        fetchAnalyticsDashboard(w).catch(() => null),
        fetchRuleAccuracy(w).catch(() => []),
        fetchProblemTypeStats(w).catch(() => null),
        fetchClassificationStats(w).catch(() => null),
        fetchVocClassificationStats(w).catch(() => null),
        fetchVocTrend(w, "group").catch(() => null),
        fetchVocMovers(w, "label", 3, prev).catch(() => null),
        fetchFixEffectiveness(w).catch(() => null),
      ]);
      setData(dash);
      setRuleAccuracy(ra);
      setPtStats(pt);
      setClsStats(cls);
      setVocStats(voc);
      setVocTrend(vocTrendRes);
      setVocMovers(vocMoversRes);
      setFixEffectiveness(fixEff);
    } catch {} finally { setLoading(false); }
  }, [resolved.dateFrom, resolved.dateTo, range.kind]);

  useEffect(() => { load(); }, [load]);

  // AI 总结按「周」「月」两种自然档位各自独立生成 + 缓存，选中同一个档位
  // 时直接展示上次生成的结果——3/6/12 月与自定义天数这类跨度模糊/易漂移的
  // 档位不支持（digestKey = null），卡片显示「暂不支持」而不是悄悄套用别的
  // 周期的数据。
  const digestKey = range.kind === "week" ? { periodType: "week" as const, periodStart: range.weekStart }
    : range.kind === "month" ? { periodType: "month" as const, periodStart: range.monthStart }
    : null;

  useEffect(() => {
    if (!digestKey) { setDigest(null); setDigestLoading(false); return; }
    setDigestLoading(true);
    fetchVocWeeklyDigest(digestKey.periodStart, digestKey.periodType)
      .then(setDigest).catch(() => setDigest(null)).finally(() => setDigestLoading(false));
  }, [digestKey?.periodType, digestKey?.periodStart]);

  const regenerateDigest = async () => {
    if (!digestKey) return;
    setDigestRegenerating(true);
    setDigestError("");
    try {
      const result = await generateVocWeeklyDigest(digestKey.periodStart, true, digestKey.periodType);
      setDigest(result);
    } catch {
      setDigestError(t("生成失败，请稍后重试"));
    } finally { setDigestRegenerating(false); }
  };

  const dailyDates = data ? Object.keys(data.daily).sort() : [];

  return (
    <div className="min-h-full">
      <header className="sticky top-0 z-10 backdrop-blur-md"
        style={{ background: "var(--j-header)", borderBottom: `1px solid ${S.border}` }}>
        <div className="flex items-center justify-between px-6 py-3">
          <div>
            <h1 className="text-base font-semibold" style={{ color: S.text1 }}>{t("数据看板")}</h1>
            <p className="text-xs mt-0.5" style={{ color: S.text3 }}>{t("项目价值 & 使用情况统计")}</p>
          </div>
          <div className="flex items-center gap-2">
            <div className="flex items-center gap-1 rounded-lg p-1" style={{ background: S.overlay }}>
              {PRESETS.map((p) => (
                <button key={p.key} onClick={() => {
                  setCustomDays("");
                  if (p.range.kind === "week") updateQuery({ week: p.range.weekStart });
                  else if (p.range.kind === "month") updateQuery({ month: p.range.monthStart });
                  else updateQuery({ days: p.range.days });
                }}
                  className="rounded-md px-3 py-1.5 text-sm font-medium transition-all"
                  style={isPresetActive(p.range) && !customDays
                    ? { background: S.surface, color: S.text1, boxShadow: "0 1px 3px rgba(0,0,0,0.1)" }
                    : { color: S.text3 }}>
                  {p.label}
                </button>
              ))}
            </div>
            <div className="flex items-center gap-1 rounded-lg p-1" style={{ background: S.overlay }}>
              <button
                onClick={() => setTaxonomyMode("voc")}
                className="rounded-md px-3 py-1.5 text-sm font-medium transition-all"
                style={taxonomyMode === "voc"
                  ? { background: S.surface, color: S.text1, boxShadow: "0 1px 3px rgba(0,0,0,0.1)" }
                  : { color: S.text3 }}>
                {t("VOC 分类")}
              </button>
              <button
                onClick={() => setTaxonomyMode("legacy")}
                className="rounded-md px-3 py-1.5 text-sm font-medium transition-all"
                style={taxonomyMode === "legacy"
                  ? { background: S.surface, color: S.text1, boxShadow: "0 1px 3px rgba(0,0,0,0.1)" }
                  : { color: S.text3 }}>
                {t("旧分类（冻结）")}
              </button>
            </div>
            <form onSubmit={(e) => {
              e.preventDefault();
              const v = parseInt(customDays);
              if (v > 0) updateQuery({ days: v });
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
                  style={{ background: S.accentBg, color: S.accent, border: "1px solid rgba(14,124,134,0.3)" }}>
                  ↵
                </button>
              )}
            </form>
          </div>
        </div>
        <div className="px-6 pb-2 -mt-1">
          <span className="text-[11px] font-mono" style={{ color: S.text3 }}>
            {t("统计区间")}: {resolved.dateFrom} ~ {resolved.dateTo}
            {resolved.inProgress && ` (${t("进行中")})`}
          </span>
        </div>
      </header>

      {loading && !data ? (
        <div className="flex items-center justify-center py-24">
          <div className="h-8 w-8 animate-spin rounded-full border-4"
            style={{ borderColor: "var(--j-border)", borderTopColor: S.accent }} />
        </div>
      ) : !data ? (
        <p className="py-24 text-center text-sm" style={{ color: S.text3 }}>{t("暂无数据")}</p>
      ) : (
        <div className="mx-auto max-w-4xl px-6 py-6 space-y-5">

          {/* Value metrics hero */}
          <section className="rounded-2xl p-6 relative overflow-hidden j-rise"
            style={{ background: "linear-gradient(135deg, var(--j-panel) 0%, var(--j-surface) 60%, var(--j-accent-soft) 100%)", border: `1px solid ${S.border}` }}>
            {/* Decorative accent */}
            <div className="absolute top-0 right-0 h-32 w-32 rounded-full opacity-10 blur-3xl"
              style={{ background: S.accent }} />
            <div className="flex items-center gap-2 mb-4">
              <span className="rounded-lg px-2 py-0.5 text-[11px] font-semibold"
                style={{ background: S.accentBg, color: S.accent, border: "1px solid rgba(14,124,134,0.25)" }}>
                {t("项目价值")}
              </span>
              <span className="text-xs" style={{ color: S.text3 }}>{resolved.dateFrom} ~ {resolved.dateTo}</span>
            </div>
            <div className="grid grid-cols-3 gap-6 relative">
              <div>
                <p className="text-4xl font-bold tabular-nums j-digits" style={{ color: S.text1 }}>
                  <CountUp value={data.value_metrics.time_saved_hours} />
                  <span className="text-xl font-normal ml-1" style={{ color: S.text3 }}>{t("小时")}</span>
                </p>
                <p className="mt-1.5 text-xs" style={{ color: S.text3 }}>{t("预估节省工时")}</p>
              </div>
              <div>
                <p className="text-4xl font-bold tabular-nums j-digits" style={{ color: S.accent }}>
                  <CountUp value={data.value_metrics.time_saved_per_ticket_min} />
                  <span className="text-xl font-normal ml-1" style={{ color: S.text3 }}>{t("分钟/单")}</span>
                </p>
                <p className="mt-1.5 text-xs" style={{ color: S.text3 }}>{t("每单节省时间")}</p>
              </div>
              <div>
                <p className="text-4xl font-bold tabular-nums j-digits" style={{ color: "#16A34A" }}>
                  <CountUp value={data.value_metrics.success_rate} />
                  <span className="text-xl font-normal ml-0.5" style={{ color: S.text3 }}>%</span>
                </p>
                <p className="mt-1.5 text-xs" style={{ color: S.text3 }}>{t("分析成功率")}</p>
              </div>
            </div>
            <p className="mt-4 text-[11px] font-mono" style={{ color: S.text3 }}>
              {t("对比")}: {t("人工处理")} ~{data.value_metrics.estimated_manual_hours}h → {t("AI 处理")} ~{data.value_metrics.estimated_ai_hours}h
            </p>
          </section>

          {/* Weekly digest summary card */}
          <section className="rounded-2xl p-6 j-rise" style={{ background: S.surface, border: `1px solid ${S.border}`, ["--d" as string]: "0.02s" }}>
            <div className="flex items-center justify-between mb-4">
              <div className="flex items-center gap-2">
                <span className="rounded-lg px-2 py-0.5 text-[11px] font-semibold"
                  style={{ background: S.accentBg, color: S.accent, border: "1px solid rgba(14,124,134,0.25)" }}>
                  {digestKey?.periodType === "month" ? t("月度总结") : t("周度总结")}
                </span>
                {digestKey && <span className="text-xs" style={{ color: S.text3 }}>{resolved.dateFrom} ~ {resolved.dateTo}</span>}
                {digestKey && resolved.inProgress && (
                  <span className="rounded-full px-2 py-0.5 text-[10px] font-medium" title={t("当前周期尚未结束，统计截至今天")}
                    style={{ background: S.overlay, color: S.text3, border: `1px solid ${S.border}` }}>
                    {t("进行中")}
                  </span>
                )}
              </div>
              {digestKey && (
                <div className="flex items-center gap-2">
                  {digestError && (
                    <span className="text-[11px]" style={{ color: "#DC2626" }}>{digestError}</span>
                  )}
                  <button
                    onClick={regenerateDigest}
                    disabled={digestRegenerating}
                    className="rounded-lg px-3 py-1.5 text-[11px] font-medium transition-all"
                    style={{ background: S.accentBg, color: S.accent, border: "1px solid rgba(14,124,134,0.3)", opacity: digestRegenerating ? 0.5 : 1 }}>
                    {digestRegenerating ? t("生成中...") : digest ? t("重新生成") : t("生成总结")}
                  </button>
                </div>
              )}
            </div>

            {!digestKey ? (
              <p className="py-6 text-center text-sm" style={{ color: S.text3 }}>{t("当前时间维度暂不支持 AI 总结（仅本周/上周/本月/上月支持）")}</p>
            ) : digestLoading ? (
              <p className="py-6 text-center text-sm" style={{ color: S.text3 }}>{t("加载中")}...</p>
            ) : !digest ? (
              <p className="py-6 text-center text-sm" style={{ color: S.text3 }}>{t("该时间段暂无总结，点击「生成总结」创建。")}</p>
            ) : (
              <div className="space-y-4">
                {digest.narrative ? (
                  <p className="text-lg font-semibold" style={{ color: S.text1 }}>{digest.narrative.headline}</p>
                ) : (
                  <p className="text-sm" style={{ color: "#DC2626" }}>{t("洞察生成失败，以下为确定性统计，可点击「重新生成」重试。")}</p>
                )}

                <p className="text-xs" style={{ color: S.text3 }}>
                  {t("本期共")} {digest.stats?.total_cur ?? 0} {t("单")}
                  {digest.stats?.total_delta_pct != null && (
                    <> · {t("环比")} {digest.stats.total_delta_pct > 0 ? "+" : ""}{digest.stats.total_delta_pct}%</>
                  )}
                </p>

                {digest.narrative && (digest.narrative.key_findings?.length ?? 0) > 0 && (
                  <div>
                    <h3 className="text-xs font-semibold mb-2" style={{ color: S.text2 }}>{t("关键发现")}</h3>
                    <ul className="space-y-1">
                      {digest.narrative.key_findings.map((f, i) => (
                        <li key={i} className="text-xs" style={{ color: S.text2 }}>
                          <span className="font-medium" style={{ color: S.text1 }}>{f.scope}</span>：{f.finding}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                {digest.narrative && (digest.narrative.product_opportunities?.length ?? 0) > 0 && (
                  <div className="rounded-xl p-4" style={{ background: S.accentBg, border: "1px solid rgba(14,124,134,0.25)" }}>
                    <h3 className="text-xs font-semibold mb-2" style={{ color: S.accent }}>{t("产品优化建议")}</h3>
                    <ul className="space-y-2">
                      {digest.narrative.product_opportunities.map((o, i) => (
                        <li key={i} className="text-xs" style={{ color: S.text1 }}>
                          <span className="font-semibold">{o.area}</span>：{o.problem} → <span className="font-medium">{o.suggestion}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                {(digest.stats?.top_movers?.length ?? 0) > 0 && (
                  <div>
                    <h3 className="text-xs font-semibold mb-2" style={{ color: S.text2 }}>{t("环比变动")}</h3>
                    <div className="space-y-1">
                      {digest.stats.top_movers.slice(0, 5).map((m) => (
                        <div key={m.key} className="flex items-center justify-between text-xs">
                          <span className="truncate" style={{ color: S.text2 }} title={m.key}>{m.key}</span>
                          <span className="font-mono tabular-nums flex-shrink-0 ml-2"
                            style={{ color: m.delta > 0 ? "#DC2626" : m.delta < 0 ? "#16A34A" : S.text3 }}>
                            {m.prev} → {m.cur} ({m.delta_pct !== null ? `${m.delta_pct > 0 ? "+" : ""}${m.delta_pct}%` : t("新增")})
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}
          </section>

          {/* Key metrics */}
          <div className="grid grid-cols-6 gap-3">
            <StatCard label={t("总分析次数")} value={data.total_analyses} index={0} />
            <StatCard label={t("分析成功")} value={data.successful_analyses} sub={`${t("其中追问")} ${data.followup_done ?? 0}`} color="#16A34A" index={1} />
            <StatCard label={t("分析失败")} value={data.failed_analyses} sub={`${t("其中追问")} ${data.followup_fail ?? 0}`} color="#DC2626" index={2} />
            <StatCard label={t("外部因素")} value={data.external_failures || 0} sub={t("额度/磁盘等")} color="#F59E0B" index={3} />
            <StatCard label={t("反馈提交")} value={data.feedback_submitted} color="#2563EB" index={4} />
            <StatCard label={t("活跃用户")} value={data.unique_users} color="#7C3AED" index={5} />
          </div>

          <div className="grid grid-cols-3 gap-3">
            <StatCard label={t("平均分析耗时")} value={`${data.avg_analysis_duration_min} ${t("分钟")}`} sub={`${data.avg_analysis_duration_ms}ms`} index={0} />
            <StatCard label={t("工单转工程师")} value={data.escalations} color={S.accent} index={1} />
            <StatCard label={t("深度分析")} value={data.event_counts.deep_analysis || 0} color="#6366F1" index={2} />
            <StatCard label={t("页面访问")} value={data.event_counts.page_visit || 0} index={3} />
          </div>

          {/* 本期总计 汇总卡 */}
          <div className="grid grid-cols-2 gap-3">
            <StatCard label={t("本期总 Token")} value={fmtTokens(data.total_tokens || 0)} sub={`${dailyDates.length} ${t("天")}`} color="#B8922E" index={0} />
            <StatCard label={t("本期总费用")} value={`$${fmtCost(data.total_cost_usd || 0)}`} sub={t("USD")} color="#16A34A" index={1} />
          </div>

          {/* 修复有效性面板 — cohort 口径为主展示（"我们修的东西真修好了吗"），
              detection 口径小字副标（"这期爆了多少复发"）——两个分子分母不同源，
              绝不能合并成一个数字。 */}
          {fixEffectiveness && fixEffectiveness.resolved_count > 0 && (
            <section className="rounded-xl p-5 j-rise" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-sm font-semibold" style={{ color: S.text1 }}>{t("修复有效性")}</h2>
                <span className="text-[11px] font-mono" style={{ color: S.text3 }}>
                  {t("本期完成")} {fixEffectiveness.resolved_count} {t("单")}
                </span>
              </div>
              <div className="grid grid-cols-4 gap-3 mb-4">
                <StatCard
                  label={t("复发率（截至今日）")}
                  value={fixEffectiveness.cohort_recurrence_rate ?? "—"}
                  sub={fixEffectiveness.cohort_recurrence_rate != null ? "%" : t("暂无数据")}
                  color={fixEffectiveness.cohort_recurrence_rate ? "#DC2626" : undefined}
                />
                <StatCard
                  label={t("本期复发命中")}
                  value={fixEffectiveness.red_hits}
                  sub={`${t("疑似复发")} · ${fixEffectiveness.recurrence_rate_by_detection ?? 0}%`}
                  color="#DC2626"
                />
                <StatCard
                  label={t("修复版本填报率")}
                  value={fixEffectiveness.fix_version_fill_rate ?? "—"}
                  sub={fixEffectiveness.fix_version_fill_rate != null ? "%" : t("暂无数据")}
                />
                <StatCard
                  label={t("历史类似（弱信号）")}
                  value={fixEffectiveness.yellow_hits}
                  color="#CA8A04"
                />
              </div>

              {fixEffectiveness.by_rule_type.length > 0 && (
                <div className="mb-4">
                  <h3 className="text-xs font-semibold mb-2" style={{ color: S.text2 }}>{t("按规则分类")}</h3>
                  <div className="space-y-1">
                    {fixEffectiveness.by_rule_type.map((r) => (
                      <div key={r.rule_type} className="flex items-center justify-between text-xs">
                        <span style={{ color: S.text2 }}>{r.rule_type}</span>
                        <span className="font-mono tabular-nums" style={{ color: r.recurred > 0 ? "#DC2626" : S.text3 }}>
                          {r.recurred} / {r.resolved}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {fixEffectiveness.top_offenders.length > 0 && (
                <div>
                  <h3 className="text-xs font-semibold mb-2" style={{ color: S.text2 }}>{t("高频复发工单")}</h3>
                  <div className="space-y-1">
                    {fixEffectiveness.top_offenders.map((o) => (
                      <a key={o.prior_issue_id} href={`/?detail=${o.prior_issue_id}`}
                        className="flex items-center justify-between text-xs rounded-lg px-2 py-1.5 hover:opacity-80"
                        style={{ background: S.overlay }}>
                        <span className="truncate" style={{ color: S.text1 }} title={o.description}>{o.description}</span>
                        <span className="font-mono flex-shrink-0 ml-2" style={{ color: "#DC2626" }}>×{o.recurrence_count}</span>
                      </a>
                    ))}
                  </div>
                </div>
              )}
            </section>
          )}

          {/* Daily trend — 双轴折线（工单数 + Token 消耗）*/}
          <section className="rounded-xl p-5 j-rise" style={{ background: S.surface, border: `1px solid ${S.border}`, ["--d" as string]: "0.08s" }}>
            <h2 className="mb-4 text-sm font-semibold" style={{ color: S.text1 }}>{t("每日趋势")}</h2>
            {dailyDates.length === 0 ? (
              <p className="py-8 text-center text-sm" style={{ color: S.text3 }}>{t("暂无数据")}</p>
            ) : (() => {
              const COUNT_COLOR = "#B8922E"; // 站点金 — 工单数（左轴）
              const TOKEN_COLOR = "#2563EB"; // 对比色 — Token 消耗（右轴）

              const dates = dailyDates;
              const counts = dates.map((d) => data.daily[d].analysis_start || 0);
              const tokens = dates.map((d) => data.daily[d].tokens || 0);

              const maxCount = Math.max(1, ...counts);
              const maxToken = Math.max(1, ...tokens);

              const W = 600, H = 220;
              const pad = { top: 10, right: 44, bottom: 24, left: 32 };
              const cw = W - pad.left - pad.right;
              const ch = H - pad.top - pad.bottom;

              const xStep = dates.length > 1 ? cw / (dates.length - 1) : 0;
              const toX = (i: number) => dates.length > 1 ? pad.left + i * xStep : pad.left + cw / 2;
              const toYCount = (v: number) => pad.top + ch - (v / maxCount) * ch;
              const toYToken = (v: number) => pad.top + ch - (v / maxToken) * ch;

              const buildPath = (pts: [number, number][]) => {
                if (pts.length === 0) return "";
                if (pts.length === 1) return `M${pts[0][0]},${pts[0][1]}`;
                let d = `M${pts[0][0]},${pts[0][1]}`;
                for (let i = 0; i < pts.length - 1; i++) {
                  const p0 = pts[Math.max(0, i - 1)];
                  const p1 = pts[i];
                  const p2 = pts[i + 1];
                  const p3 = pts[Math.min(pts.length - 1, i + 2)];
                  const cp1x = p1[0] + (p2[0] - p0[0]) / 6;
                  const cp1y = p1[1] + (p2[1] - p0[1]) / 6;
                  const cp2x = p2[0] - (p3[0] - p1[0]) / 6;
                  const cp2y = p2[1] - (p3[1] - p1[1]) / 6;
                  d += ` C${cp1x},${cp1y} ${cp2x},${cp2y} ${p2[0]},${p2[1]}`;
                }
                return d;
              };

              const labelInterval = Math.max(1, Math.floor(dates.length / 6));

              // 左轴网格刻度（工单数）
              const countGridStep = maxCount <= 5 ? 1 : maxCount <= 20 ? 5 : Math.ceil(maxCount / 4 / 5) * 5;
              const countTop = Math.ceil(maxCount / countGridStep) * countGridStep || 1;

              // 右轴刻度（token）— 与左轴同数量的刻度行对齐
              const gridLines = Math.max(1, Math.floor(countTop / countGridStep));

              const countPts: [number, number][] = dates.map((d, i) => [toX(i), toYCount(counts[i])]);
              const tokenPts: [number, number][] = dates.map((d, i) => [toX(i), toYToken(tokens[i])]);

              // token 轴标签简写
              const fmtTokenAxis = (v: number) =>
                v >= 1_000_000 ? `${(v / 1_000_000).toFixed(1)}M`
                : v >= 1_000 ? `${(v / 1_000).toFixed(0)}k`
                : `${Math.round(v)}`;

              return (
                <div>
                  {/* Legend */}
                  <div className="flex flex-wrap gap-x-4 gap-y-1 mb-2">
                    <div className="flex items-center gap-1">
                      <span className="inline-block h-2 w-2 rounded-full" style={{ background: COUNT_COLOR }} />
                      <span className="text-[10px]" style={{ color: S.text3 }}>{t("工单数")}</span>
                    </div>
                    <div className="flex items-center gap-1">
                      <span className="inline-block h-2 w-2 rounded-full" style={{ background: TOKEN_COLOR }} />
                      <span className="text-[10px]" style={{ color: S.text3 }}>{t("Token 消耗")}</span>
                    </div>
                  </div>
                  <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ overflow: "visible" }}>
                    {/* 网格线 + 双轴标签 */}
                    {Array.from({ length: gridLines + 1 }, (_, i) => {
                      const cv = i * countGridStep;
                      const y = toYCount(cv);
                      const tv = (cv / countTop) * maxToken;
                      return (
                        <g key={i}>
                          <line x1={pad.left} x2={W - pad.right} y1={y} y2={y}
                            stroke={S.border} strokeWidth={0.5} />
                          {/* 左轴：工单数 */}
                          <text x={pad.left - 4} y={y + 3} textAnchor="end"
                            style={{ fontSize: 8, fill: COUNT_COLOR, fontFamily: "monospace" }}>{cv}</text>
                          {/* 右轴：token */}
                          <text x={W - pad.right + 4} y={y + 3} textAnchor="start"
                            style={{ fontSize: 8, fill: TOKEN_COLOR, fontFamily: "monospace" }}>{fmtTokenAxis(tv)}</text>
                        </g>
                      );
                    })}
                    {/* Token 线（右轴）*/}
                    <path d={buildPath(tokenPts)} fill="none" stroke={TOKEN_COLOR}
                      strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" opacity={0.85} />
                    {/* 工单数线（左轴）*/}
                    <path d={buildPath(countPts)} fill="none" stroke={COUNT_COLOR}
                      strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" opacity={0.9} />
                    {/* 数据点 + hover tooltip */}
                    {dates.map((d, i) => {
                      const day = data.daily[d];
                      const cnt = counts[i];
                      const tok = tokens[i];
                      const cost = day.cost_usd || 0;
                      const success = day.analysis_done || 0;
                      const fail = day.analysis_fail || 0;
                      const tip = `${d} · ${cnt} ${t("工单")} · ${fmtTokens(tok)} tok · $${fmtCost(cost)}\n${success}✓${fail > 0 ? ` ${fail}✗` : ""}`;
                      return (
                        <g key={d}>
                          {/* token 点 */}
                          <circle cx={toX(i)} cy={toYToken(tok)} r={2.5}
                            fill="#fff" stroke={TOKEN_COLOR} strokeWidth={1.5}>
                            <title>{tip}</title>
                          </circle>
                          {/* 工单数点 */}
                          <circle cx={toX(i)} cy={toYCount(cnt)} r={2.5}
                            fill="#fff" stroke={COUNT_COLOR} strokeWidth={1.5}>
                            <title>{tip}</title>
                          </circle>
                          {/* 透明 hover 命中区（整条竖列）*/}
                          <rect x={toX(i) - Math.max(6, xStep / 2)} y={pad.top}
                            width={Math.max(12, xStep)} height={ch}
                            fill="transparent">
                            <title>{tip}</title>
                          </rect>
                        </g>
                      );
                    })}
                    {/* X labels */}
                    {dates.map((d, i) => {
                      if (i % labelInterval !== 0 && i !== dates.length - 1) return null;
                      return (
                        <text key={d} x={toX(i)} y={H - 4} textAnchor="middle"
                          style={{ fontSize: 8, fill: S.text3, fontFamily: "monospace" }}>{d.slice(5)}</text>
                      );
                    })}
                  </svg>
                </div>
              );
            })()}
          </section>

          {/* VOC Portal taxonomy: Top 10 (group › label) + multi-line trend (top 6 groups) */}
          {taxonomyMode === "voc" && vocStats && vocStats.groups.length > 0 && (() => {
            // Flatten group>label into a single ranked list for Top 10 — L1 alone is
            // too coarse (11 groups), L3 too sparse at ~400 tickets/month.
            const flat: { key: string; count: number }[] = [];
            for (const g of vocStats.groups) {
              for (const l of g.labels) {
                flat.push({ key: l.label ? `${g.group} › ${l.label}` : g.group, count: l.count });
              }
            }
            flat.sort((a, b) => b.count - a.count);
            const top10 = flat.slice(0, 10);
            const maxCount = top10[0]?.count || 1;
            const COLORS = ["#0E7C86","#2563EB","#16A34A","#DC2626","#7C3AED","#EA580C","#0891B2","#DB2777","#4F46E5","#65A30D"];

            const trendDates = vocTrend ? Object.keys(vocTrend.trend).sort() : [];
            const topGroups = vocStats.groups.slice(0, 6).map((g) => g.group);

            return (
              <div className="grid grid-cols-2 gap-4 j-rise" style={{ ["--d" as string]: "0.12s" }}>
                <section className="rounded-xl p-5" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
                  <div className="flex items-center justify-between mb-4">
                    <h2 className="text-sm font-semibold" style={{ color: S.text1 }}>{t("VOC 分类 Top 10")}</h2>
                    <span className="text-[11px] font-mono" style={{ color: S.text3 }}>
                      {t("共")} {flat.length} {t("类")} / {vocStats.total_tagged} {t("单已打标")}
                    </span>
                  </div>
                  <div className="space-y-2">
                    {top10.map((item, i) => {
                      const pct = Math.max(4, (item.count / maxCount) * 100);
                      return (
                        <div key={item.key} className="flex items-center gap-2">
                          <span className="flex h-5 w-5 items-center justify-center rounded-full text-[10px] font-bold flex-shrink-0"
                            style={{ background: `${COLORS[i]}15`, color: COLORS[i] }}>
                            {i + 1}
                          </span>
                          <div className="flex-1 min-w-0">
                            <div className="flex items-center justify-between mb-0.5">
                              <span className="text-xs truncate" style={{ color: S.text2 }} title={item.key}>{item.key}</span>
                              <span className="text-xs tabular-nums font-mono flex-shrink-0 ml-2" style={{ color: S.text1 }}>
                                {item.count}
                              </span>
                            </div>
                            <div className="h-3 w-full overflow-hidden rounded-full" style={{ background: S.hover }}>
                              <div className="h-full rounded-full transition-all duration-700"
                                style={{ width: `${pct}%`, background: COLORS[i], opacity: 0.75 }} />
                            </div>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </section>

                <section className="rounded-xl p-5" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
                  <h2 className="mb-4 text-sm font-semibold" style={{ color: S.text1 }}>{t("VOC 分类趋势")}</h2>
                  {trendDates.length < 2 ? (
                    <p className="py-8 text-center text-sm" style={{ color: S.text3 }}>{t("暂无数据")}</p>
                  ) : (() => {
                    let maxY = 1;
                    for (const d of trendDates) {
                      for (const g of topGroups) {
                        const v = vocTrend!.trend[d]?.[g] || 0;
                        if (v > maxY) maxY = v;
                      }
                    }
                    const gridStep = maxY <= 5 ? 1 : maxY <= 20 ? 5 : Math.ceil(maxY / 4 / 5) * 5;
                    maxY = Math.ceil(maxY / gridStep) * gridStep;

                    const W = 400, H = 200;
                    const pad = { top: 8, right: 12, bottom: 22, left: 28 };
                    const cw = W - pad.left - pad.right;
                    const ch = H - pad.top - pad.bottom;
                    const xStep = trendDates.length > 1 ? cw / (trendDates.length - 1) : 0;
                    const toX = (i: number) => pad.left + i * xStep;
                    const toY = (v: number) => pad.top + ch - (v / maxY) * ch;

                    const buildPath = (pts: [number, number][]) => {
                      if (pts.length < 2) return "";
                      let d = `M${pts[0][0]},${pts[0][1]}`;
                      for (let i = 0; i < pts.length - 1; i++) {
                        const p0 = pts[Math.max(0, i - 1)];
                        const p1 = pts[i];
                        const p2 = pts[i + 1];
                        const p3 = pts[Math.min(pts.length - 1, i + 2)];
                        const cp1x = p1[0] + (p2[0] - p0[0]) / 6;
                        const cp1y = p1[1] + (p2[1] - p0[1]) / 6;
                        const cp2x = p2[0] - (p3[0] - p1[0]) / 6;
                        const cp2y = p2[1] - (p3[1] - p1[1]) / 6;
                        d += ` C${cp1x},${cp1y} ${cp2x},${cp2y} ${p2[0]},${p2[1]}`;
                      }
                      return d;
                    };
                    const labelInterval = Math.max(1, Math.floor(trendDates.length / 6));

                    return (
                      <div>
                        <div className="flex flex-wrap gap-x-3 gap-y-1 mb-2">
                          {topGroups.map((g, i) => (
                            <div key={g} className="flex items-center gap-1">
                              <span className="inline-block h-2 w-2 rounded-full" style={{ background: COLORS[i] }} />
                              <span className="text-[10px]" style={{ color: S.text3 }}>{g}</span>
                            </div>
                          ))}
                        </div>
                        <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ overflow: "visible" }}>
                          {Array.from({ length: Math.floor(maxY / gridStep) + 1 }, (_, i) => {
                            const v = i * gridStep;
                            const y = toY(v);
                            return (
                              <g key={v}>
                                <line x1={pad.left} x2={W - pad.right} y1={y} y2={y} stroke={S.border} strokeWidth={0.5} />
                                <text x={pad.left - 4} y={y + 3} textAnchor="end"
                                  style={{ fontSize: 8, fill: S.text3, fontFamily: "monospace" }}>{v}</text>
                              </g>
                            );
                          })}
                          {topGroups.map((g, gi) => {
                            const pts: [number, number][] = trendDates.map((d, di) => [toX(di), toY(vocTrend!.trend[d]?.[g] || 0)]);
                            return (
                              <path key={g} d={buildPath(pts)} fill="none" stroke={COLORS[gi]} strokeWidth={1.8}
                                strokeLinecap="round" strokeLinejoin="round" opacity={0.85} />
                            );
                          })}
                          {topGroups.map((g, gi) =>
                            trendDates.map((d, di) => {
                              const v = vocTrend!.trend[d]?.[g] || 0;
                              if (v === 0) return null;
                              return (
                                <circle key={`${gi}-${di}`} cx={toX(di)} cy={toY(v)} r={2.5} fill="#fff" stroke={COLORS[gi]} strokeWidth={1.5}>
                                  <title>{`${d} ${g}: ${v}`}</title>
                                </circle>
                              );
                            })
                          )}
                          {trendDates.map((d, i) => {
                            if (i % labelInterval !== 0 && i !== trendDates.length - 1) return null;
                            return (
                              <text key={d} x={toX(i)} y={H - 2} textAnchor="middle"
                                style={{ fontSize: 8, fill: S.text3, fontFamily: "monospace" }}>{d.slice(5)}</text>
                            );
                          })}
                        </svg>
                      </div>
                    );
                  })()}
                </section>
              </div>
            );
          })()}

          {/* Problem type distribution + trend (legacy, frozen for comparison) */}
          {taxonomyMode === "legacy" && ptStats && ptStats.top10.length > 0 && (() => {
            const top10 = ptStats.top10;
            const maxCount = top10[0]?.count || 1;
            const trendDates = Object.keys(ptStats.trend).sort();
            const COLORS = ["#0E7C86","#2563EB","#16A34A","#DC2626","#7C3AED","#EA580C","#0891B2","#DB2777","#4F46E5","#65A30D"];
            return (
              <div className="grid grid-cols-2 gap-4 j-rise" style={{ ["--d" as string]: "0.12s" }}>
                {/* Top 10 bar chart */}
                <section className="rounded-xl p-5" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
                  <div className="flex items-center justify-between mb-4">
                    <h2 className="text-sm font-semibold" style={{ color: S.text1 }}>{t("问题分类 Top 10")}</h2>
                    <span className="text-[11px] font-mono" style={{ color: S.text3 }}>
                      {t("共")} {ptStats.distribution.length} {t("类")} / {ptStats.total} {t("单")}
                    </span>
                  </div>
                  <div className="space-y-2">
                    {top10.map((item, i) => {
                      const pct = Math.max(4, (item.count / maxCount) * 100);
                      return (
                        <div key={item.problem_type} className="flex items-center gap-2">
                          <span className="flex h-5 w-5 items-center justify-center rounded-full text-[10px] font-bold flex-shrink-0"
                            style={{ background: `${COLORS[i]}15`, color: COLORS[i] }}>
                            {i + 1}
                          </span>
                          <div className="flex-1 min-w-0">
                            <div className="flex items-center justify-between mb-0.5">
                              <span className="text-xs truncate" style={{ color: S.text2 }}>{item.problem_type}</span>
                              <span className="text-xs tabular-nums font-mono flex-shrink-0 ml-2" style={{ color: S.text1 }}>
                                {item.count}
                              </span>
                            </div>
                            <div className="h-3 w-full overflow-hidden rounded-full" style={{ background: S.hover }}>
                              <div className="h-full rounded-full transition-all duration-700"
                                style={{ width: `${pct}%`, background: COLORS[i], opacity: 0.75 }} />
                            </div>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </section>

                {/* Trend line chart */}
                <section className="rounded-xl p-5" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
                  <h2 className="mb-4 text-sm font-semibold" style={{ color: S.text1 }}>{t("问题分类趋势")}</h2>
                  {(() => {
                    const dates = Object.keys(ptStats.trend).sort();
                    if (dates.length < 2) return <p className="py-8 text-center text-sm" style={{ color: S.text3 }}>{t("暂无数据")}</p>;

                    const series = top10.slice(0, 6);
                    // Compute max Y across all series
                    let maxY = 1;
                    for (const d of dates) {
                      for (const s of series) {
                        const v = ptStats.trend[d]?.[s.problem_type] || 0;
                        if (v > maxY) maxY = v;
                      }
                    }
                    // Round maxY up for nicer grid
                    const gridStep = maxY <= 5 ? 1 : maxY <= 20 ? 5 : Math.ceil(maxY / 4 / 5) * 5;
                    maxY = Math.ceil(maxY / gridStep) * gridStep;

                    const W = 400, H = 200;
                    const pad = { top: 8, right: 12, bottom: 22, left: 28 };
                    const cw = W - pad.left - pad.right;
                    const ch = H - pad.top - pad.bottom;

                    const xStep = dates.length > 1 ? cw / (dates.length - 1) : 0;
                    const toX = (i: number) => pad.left + i * xStep;
                    const toY = (v: number) => pad.top + ch - (v / maxY) * ch;

                    // Build smooth paths using catmull-rom → cubic bezier approximation
                    const buildPath = (pts: [number, number][]) => {
                      if (pts.length < 2) return "";
                      let d = `M${pts[0][0]},${pts[0][1]}`;
                      for (let i = 0; i < pts.length - 1; i++) {
                        const p0 = pts[Math.max(0, i - 1)];
                        const p1 = pts[i];
                        const p2 = pts[i + 1];
                        const p3 = pts[Math.min(pts.length - 1, i + 2)];
                        const cp1x = p1[0] + (p2[0] - p0[0]) / 6;
                        const cp1y = p1[1] + (p2[1] - p0[1]) / 6;
                        const cp2x = p2[0] - (p3[0] - p1[0]) / 6;
                        const cp2y = p2[1] - (p3[1] - p1[1]) / 6;
                        d += ` C${cp1x},${cp1y} ${cp2x},${cp2y} ${p2[0]},${p2[1]}`;
                      }
                      return d;
                    };

                    // X-axis labels: show ~5-7 evenly spaced labels
                    const labelInterval = Math.max(1, Math.floor(dates.length / 6));

                    return (
                      <div>
                        {/* Legend */}
                        <div className="flex flex-wrap gap-x-3 gap-y-1 mb-2">
                          {series.map((item, i) => (
                            <div key={item.problem_type} className="flex items-center gap-1">
                              <span className="inline-block h-2 w-2 rounded-full" style={{ background: COLORS[i] }} />
                              <span className="text-[10px]" style={{ color: S.text3 }}>{item.problem_type}</span>
                            </div>
                          ))}
                        </div>
                        {/* SVG chart */}
                        <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ overflow: "visible" }}>
                          {/* Y grid lines + labels */}
                          {Array.from({ length: Math.floor(maxY / gridStep) + 1 }, (_, i) => {
                            const v = i * gridStep;
                            const y = toY(v);
                            return (
                              <g key={v}>
                                <line x1={pad.left} x2={W - pad.right} y1={y} y2={y}
                                  stroke={S.border} strokeWidth={0.5} />
                                <text x={pad.left - 4} y={y + 3} textAnchor="end"
                                  style={{ fontSize: 8, fill: S.text3, fontFamily: "monospace" }}>{v}</text>
                              </g>
                            );
                          })}
                          {/* Lines */}
                          {series.map((item, si) => {
                            const pts: [number, number][] = dates.map((d, di) => [
                              toX(di), toY(ptStats.trend[d]?.[item.problem_type] || 0),
                            ]);
                            return (
                              <path key={item.problem_type} d={buildPath(pts)}
                                fill="none" stroke={COLORS[si]} strokeWidth={1.8}
                                strokeLinecap="round" strokeLinejoin="round" opacity={0.85} />
                            );
                          })}
                          {/* Dots */}
                          {series.map((item, si) =>
                            dates.map((d, di) => {
                              const v = ptStats.trend[d]?.[item.problem_type] || 0;
                              if (v === 0) return null;
                              return (
                                <circle key={`${si}-${di}`} cx={toX(di)} cy={toY(v)} r={2.5}
                                  fill="#fff" stroke={COLORS[si]} strokeWidth={1.5}>
                                  <title>{`${d} ${item.problem_type}: ${v}`}</title>
                                </circle>
                              );
                            })
                          )}
                          {/* X labels */}
                          {dates.map((d, i) => {
                            if (i % labelInterval !== 0 && i !== dates.length - 1) return null;
                            return (
                              <text key={d} x={toX(i)} y={H - 2} textAnchor="middle"
                                style={{ fontSize: 8, fill: S.text3, fontFamily: "monospace" }}>{d.slice(5)}</text>
                            );
                          })}
                        </svg>
                      </div>
                    );
                  })()}
                </section>
              </div>
            );
          })()}

          {/* VOC Portal taxonomy: donut chart (new, default) */}
          {taxonomyMode === "voc" && vocStats && vocStats.groups.length > 0 && (() => {
            const PIE_COLORS = ["#0E7C86","#2563EB","#16A34A","#DC2626","#7C3AED","#EA580C","#0891B2","#DB2777","#4F46E5","#65A30D","#D97706"];
            const total = vocStats.total_tagged;
            const R = 100, cx = 120, cy = 120;
            let angle = 0;
            const slices = vocStats.groups.map((g, i) => {
              const pct = total > 0 ? g.count / total : 0;
              const startAngle = angle;
              angle += pct * 360;
              const endAngle = angle;
              const large = pct > 0.5 ? 1 : 0;
              const rad1 = (startAngle - 90) * Math.PI / 180;
              const rad2 = (endAngle - 90) * Math.PI / 180;
              const x1 = cx + R * Math.cos(rad1), y1 = cy + R * Math.sin(rad1);
              const x2 = cx + R * Math.cos(rad2), y2 = cy + R * Math.sin(rad2);
              const d = pct >= 1
                ? `M${cx},${cy - R} A${R},${R} 0 1,1 ${cx},${cy + R} A${R},${R} 0 1,1 ${cx},${cy - R}Z`
                : `M${cx},${cy} L${x1},${y1} A${R},${R} 0 ${large},1 ${x2},${y2} Z`;
              return { d, color: PIE_COLORS[i % PIE_COLORS.length], group: g.group, count: g.count, pct };
            });

            return (
              <section className="rounded-xl p-5 j-rise" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
                <h2 className="mb-4 text-sm font-semibold" style={{ color: S.text1 }}>{t("VOC 分类占比")}</h2>
                <div className="grid grid-cols-2 gap-6">
                  <div className="flex items-center justify-center">
                    <svg viewBox="0 0 240 240" className="w-full max-w-[240px]">
                      {slices.map((s, i) => (
                        <path key={i} d={s.d} fill={s.color} opacity={0.85}
                          className="transition-opacity hover:opacity-100 cursor-pointer"
                          onClick={() => setExpandedVocGroup(expandedVocGroup === s.group ? null : s.group)}>
                          <title>{`${s.group}: ${s.count} (${(s.pct * 100).toFixed(1)}%)`}</title>
                        </path>
                      ))}
                      <circle cx={cx} cy={cy} r={50} fill="var(--j-surface)" />
                      <text x={cx} y={cy - 6} textAnchor="middle" style={{ fontSize: 18, fontWeight: 700, fill: S.text1 }}>{total}</text>
                      <text x={cx} y={cy + 12} textAnchor="middle" style={{ fontSize: 9, fill: S.text3 }}>{t("已打标工单")}</text>
                    </svg>
                  </div>
                  <div className="space-y-1 max-h-[320px] overflow-y-auto pr-1">
                    {vocStats.groups.map((g, i) => {
                      const pct = total > 0 ? (g.count / total * 100).toFixed(1) : "0";
                      return (
                        <div key={g.group} className="flex items-center gap-2 rounded-lg px-2 py-1.5">
                          <span className="inline-block h-2.5 w-2.5 rounded-sm flex-shrink-0" style={{ background: PIE_COLORS[i % PIE_COLORS.length] }} />
                          <span className="text-xs flex-1 truncate" style={{ color: S.text2 }}>{g.group}</span>
                          <span className="text-[11px] font-mono tabular-nums flex-shrink-0" style={{ color: S.text1 }}>{g.count}</span>
                          <span className="text-[10px] font-mono flex-shrink-0 w-12 text-right" style={{ color: S.text3 }}>{pct}%</span>
                        </div>
                      );
                    })}
                  </div>
                </div>
              </section>
            );
          })()}

          {/* VOC movers: week-over-week diverging bar chart */}
          {taxonomyMode === "voc" && vocMovers && vocMovers.movers.length > 0 && (() => {
            const maxAbs = Math.max(1, ...vocMovers.movers.map((m) => Math.abs(m.delta)));
            const top = vocMovers.movers.slice(0, 10);
            return (
              <section className="rounded-xl p-5 j-rise" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
                <div className="flex items-center justify-between mb-4">
                  <h2 className="text-sm font-semibold" style={{ color: S.text1 }}>{t("周环比变动")}</h2>
                  <span className="text-[11px] font-mono" style={{ color: S.text3 }}>
                    {vocMovers.prev_from} ~ {vocMovers.prev_to} → {vocMovers.cur_from} ~ {vocMovers.cur_to}
                  </span>
                </div>
                <div className="space-y-2">
                  {top.map((m) => {
                    const widthPct = (Math.abs(m.delta) / maxAbs) * 50; // half-width max, diverges from center
                    const isUp = m.delta > 0;
                    return (
                      <div key={m.key} className="flex items-center gap-2">
                        <span className="text-xs w-1/3 truncate text-right" style={{ color: S.text2 }} title={m.key}>{m.key}</span>
                        <div className="flex-1 flex items-center h-4" style={{ position: "relative" }}>
                          <div className="absolute left-1/2 top-0 bottom-0 w-px" style={{ background: S.border }} />
                          <div className="h-full rounded"
                            style={{
                              position: "absolute",
                              left: isUp ? "50%" : `${50 - widthPct}%`,
                              width: `${widthPct}%`,
                              background: isUp ? "#DC2626" : "#16A34A",
                              opacity: 0.75,
                            }} />
                        </div>
                        <span className="text-[11px] font-mono tabular-nums w-20 flex-shrink-0"
                          style={{ color: isUp ? "#DC2626" : "#16A34A" }}>
                          {m.prev}→{m.cur} ({m.delta_pct !== null ? `${m.delta_pct > 0 ? "+" : ""}${m.delta_pct}%` : t("新增")})
                        </span>
                      </div>
                    );
                  })}
                </div>
              </section>
            );
          })()}

          {/* VOC Portal taxonomy: group → label → diagnosis drill-down (new, default) */}
          {taxonomyMode === "voc" && vocStats && vocStats.groups.length > 0 && (() => {
            const totalTagged = vocStats.total_tagged;
            return (
              <section className="rounded-xl p-5 j-rise" style={{ background: S.surface, border: `1px solid ${S.border}`, ["--d" as string]: "0.16s" }}>
                <div className="flex items-center justify-between mb-4">
                  <h2 className="text-sm font-semibold" style={{ color: S.text1 }}>{t("VOC 分类分布")}</h2>
                  <span className="text-[11px] font-mono" style={{ color: S.text3 }}>
                    {t("共")} {vocStats.groups.length} {t("个分类组")} / {totalTagged} {t("单已打标")}
                    {vocStats.total > totalTagged && (
                      <> · {vocStats.total - totalTagged} {t("单未打标")}</>
                    )}
                  </span>
                </div>
                <div className="space-y-1">
                  {vocStats.groups.map((g) => {
                    const isExpanded = expandedVocGroup === g.group;
                    const pct = totalTagged > 0 ? (g.count / totalTagged * 100).toFixed(1) : "0";
                    return (
                      <div key={g.group}>
                        <button
                          onClick={() => setExpandedVocGroup(isExpanded ? null : g.group)}
                          className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 transition-colors text-left"
                          onMouseEnter={(e) => (e.currentTarget.style.background = S.overlay)}
                          onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}>
                          <span className="text-[9px] flex-shrink-0" style={{ color: S.text3 }}>{isExpanded ? "▼" : "▶"}</span>
                          <span className="text-xs flex-1 truncate font-medium" style={{ color: S.text2 }}>{g.group}</span>
                          <span className="text-[11px] font-mono tabular-nums flex-shrink-0" style={{ color: S.text1 }}>{g.count}</span>
                          <span className="text-[10px] font-mono flex-shrink-0 w-12 text-right" style={{ color: S.text3 }}>{pct}%</span>
                        </button>
                        {isExpanded && (
                          <div className="ml-6 mt-0.5 mb-1 space-y-1">
                            {g.labels.map((l) => (
                              <div key={l.label} className="rounded px-2 py-1" style={{ background: "var(--j-hover)" }}>
                                <div className="flex items-center justify-between">
                                  <span className="text-[11px] font-medium" style={{ color: S.text2 }}>{l.label || t("(无二级标签)")}</span>
                                  <span className="text-[11px] font-mono tabular-nums" style={{ color: S.text1 }}>{l.count}</span>
                                </div>
                                <div className="mt-1 flex flex-wrap gap-1.5">
                                  {l.diagnoses.map((d) => (
                                    <span key={d.diagnosis} title={d.diagnosis}
                                      className="rounded-full px-2 py-0.5 text-[10px]"
                                      style={{ background: S.accentBg, color: S.accent, border: "1px solid rgba(14,124,134,0.25)" }}>
                                      {d.diagnosis || t("(无三级诊断)")} · {d.count}
                                    </span>
                                  ))}
                                </div>
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </section>
            );
          })()}
          {taxonomyMode === "voc" && (!vocStats || vocStats.groups.length === 0) && (
            <section className="rounded-xl p-5 j-rise" style={{ background: S.surface, border: `1px solid ${S.border}`, ["--d" as string]: "0.16s" }}>
              <p className="text-xs" style={{ color: S.text3 }}>
                {t("暂无 VOC 分类数据——taxonomy 尚未同步，或所选时间范围内还没有打标结果。")}
              </p>
            </section>
          )}

          {/* Classification: Pie chart + Device breakdown (legacy, frozen for comparison) */}
          {taxonomyMode === "legacy" && clsStats && clsStats.category_distribution.length > 0 && (() => {
            const PIE_COLORS = [
              "#0E7C86","#2563EB","#16A34A","#DC2626","#7C3AED","#EA580C",
              "#0891B2","#DB2777","#4F46E5","#65A30D","#D97706","#059669",
              "#6366F1","#E11D48","#0284C7","#7C2D12","#4338CA","#BE185D",
            ];
            const cats = clsStats.category_distribution;
            const totalCat = cats.reduce((s, c) => s + c.count, 0);
            const devices = clsStats.device_distribution.filter(d => d.device_type !== "未知" || clsStats.device_distribution.length === 1);
            const deviceTabs = [{ device_type: "all", count: clsStats.total, categories: [] as { category: string; count: number }[] }, ...devices];

            // Build pie chart SVG
            const R = 100, cx = 120, cy = 120;
            let angle = 0;
            const slices = cats.map((c, i) => {
              const pct = totalCat > 0 ? c.count / totalCat : 0;
              const startAngle = angle;
              angle += pct * 360;
              const endAngle = angle;
              const large = pct > 0.5 ? 1 : 0;
              const rad1 = (startAngle - 90) * Math.PI / 180;
              const rad2 = (endAngle - 90) * Math.PI / 180;
              const x1 = cx + R * Math.cos(rad1);
              const y1 = cy + R * Math.sin(rad1);
              const x2 = cx + R * Math.cos(rad2);
              const y2 = cy + R * Math.sin(rad2);
              const d = pct >= 1
                ? `M${cx},${cy - R} A${R},${R} 0 1,1 ${cx},${cy + R} A${R},${R} 0 1,1 ${cx},${cy - R}Z`
                : `M${cx},${cy} L${x1},${y1} A${R},${R} 0 ${large},1 ${x2},${y2} Z`;
              return { d, color: PIE_COLORS[i % PIE_COLORS.length], category: c.category, count: c.count, pct };
            });

            // Filter categories by device
            const filteredCats = selectedDevice === "all"
              ? cats
              : (() => {
                  const dev = devices.find(d => d.device_type === selectedDevice);
                  if (!dev) return cats;
                  const devCatMap = new Map(dev.categories.map(c => [c.category, c.count]));
                  return cats
                    .map(c => ({ ...c, count: devCatMap.get(c.category) || 0 }))
                    .filter(c => c.count > 0)
                    .sort((a, b) => b.count - a.count);
                })();
            const filteredTotal = filteredCats.reduce((s, c) => s + c.count, 0);

            return (
              <section className="rounded-xl p-5 j-rise" style={{ background: S.surface, border: `1px solid ${S.border}`, ["--d" as string]: "0.16s" }}>
                <div className="flex items-center justify-between mb-4">
                  <div className="flex items-center gap-3">
                    <h2 className="text-sm font-semibold" style={{ color: S.text1 }}>{t("问题分类分布")}</h2>
                    <span className="text-[11px] font-mono" style={{ color: S.text3 }}>
                      {t("共")} {cats.length} {t("类")} / {totalCat} {t("单")}
                      {clsStats.total_with_categories > 0 && (
                        <> · {clsStats.total_with_categories} {t("条已分类")}</>
                      )}
                    </span>
                  </div>
                  <div className="flex items-center gap-2">
                    {clsStats.total_with_categories < clsStats.total && (
                      <button
                        onClick={async () => {
                          setBackfilling(true);
                          try {
                            const res = await backfillClassifications(1000);
                            if (res.updated > 0) load();
                          } catch {} finally { setBackfilling(false); }
                        }}
                        disabled={backfilling}
                        className="rounded-lg px-3 py-1.5 text-[11px] font-medium transition-all"
                        style={{ background: S.accentBg, color: S.accent, border: "1px solid rgba(14,124,134,0.3)", opacity: backfilling ? 0.5 : 1 }}>
                        {backfilling ? t("回溯中...") : t("回溯分类")}
                      </button>
                    )}
                  </div>
                </div>

                {/* Device type tabs */}
                {devices.length > 1 && (
                  <div className="flex items-center gap-1 mb-4 rounded-lg p-1" style={{ background: S.overlay }}>
                    {deviceTabs.map((d) => (
                      <button key={d.device_type}
                        onClick={() => setSelectedDevice(d.device_type)}
                        className="rounded-md px-3 py-1.5 text-xs font-medium transition-all"
                        style={selectedDevice === d.device_type
                          ? { background: S.surface, color: S.text1, boxShadow: "0 1px 3px rgba(0,0,0,0.1)" }
                          : { color: S.text3 }}>
                        {d.device_type === "all" ? t("全部设备") : d.device_type}
                        <span className="ml-1 font-mono text-[10px]" style={{ color: S.text3 }}>({d.count})</span>
                      </button>
                    ))}
                  </div>
                )}

                <div className="grid grid-cols-2 gap-6">
                  {/* Pie chart */}
                  <div className="flex items-center justify-center">
                    <svg viewBox="0 0 240 240" className="w-full max-w-[240px]">
                      {slices.map((s, i) => (
                        <path key={i} d={s.d} fill={s.color} opacity={0.85}
                          className="transition-opacity hover:opacity-100 cursor-pointer"
                          onClick={() => setExpandedCat(expandedCat === s.category ? null : s.category)}>
                          <title>{`${s.category}: ${s.count} (${(s.pct * 100).toFixed(1)}%)`}</title>
                        </path>
                      ))}
                      {/* Center hole for donut */}
                      <circle cx={cx} cy={cy} r={50} fill="var(--j-surface)" />
                      <text x={cx} y={cy - 6} textAnchor="middle" style={{ fontSize: 18, fontWeight: 700, fill: S.text1 }}>
                        {filteredTotal}
                      </text>
                      <text x={cx} y={cy + 12} textAnchor="middle" style={{ fontSize: 9, fill: S.text3 }}>
                        {t("问题总数")}
                      </text>
                    </svg>
                  </div>

                  {/* Legend + expandable subcategories */}
                  <div className="space-y-1 max-h-[320px] overflow-y-auto pr-1">
                    {filteredCats.map((c, i) => {
                      const pct = filteredTotal > 0 ? (c.count / filteredTotal * 100).toFixed(1) : "0";
                      const isExpanded = expandedCat === c.category;
                      const origCat = cats.find(oc => oc.category === c.category);
                      const subcats = origCat?.subcategories || [];
                      return (
                        <div key={c.category}>
                          <button
                            onClick={() => setExpandedCat(isExpanded ? null : c.category)}
                            className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 transition-colors text-left"
                            onMouseEnter={(e) => (e.currentTarget.style.background = S.overlay)}
                            onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}>
                            <span className="inline-block h-2.5 w-2.5 rounded-sm flex-shrink-0"
                              style={{ background: PIE_COLORS[cats.findIndex(oc => oc.category === c.category) % PIE_COLORS.length] }} />
                            <span className="text-xs flex-1 truncate" style={{ color: S.text2 }}>{c.category}</span>
                            <span className="text-[11px] font-mono tabular-nums flex-shrink-0" style={{ color: S.text1 }}>
                              {c.count}
                            </span>
                            <span className="text-[10px] font-mono flex-shrink-0 w-12 text-right" style={{ color: S.text3 }}>
                              {pct}%
                            </span>
                            {subcats.length > 0 && (
                              <span className="text-[9px] flex-shrink-0" style={{ color: S.text3 }}>
                                {isExpanded ? "▼" : "▶"}
                              </span>
                            )}
                          </button>
                          {isExpanded && subcats.length > 0 && (
                            <div className="ml-6 mt-0.5 mb-1 space-y-0.5">
                              {subcats.map((sc) => (
                                <div key={sc.subcategory} className="flex items-center justify-between px-2 py-1 rounded"
                                  style={{ background: "var(--j-hover)" }}>
                                  <span className="text-[11px]" style={{ color: S.text3 }}>{sc.subcategory}</span>
                                  <span className="text-[11px] font-mono tabular-nums" style={{ color: S.text2 }}>{sc.count}</span>
                                </div>
                              ))}
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </div>

                {/* Device breakdown summary */}
                {devices.length > 1 && selectedDevice === "all" && (
                  <div className="mt-4 pt-4" style={{ borderTop: `1px solid ${S.border}` }}>
                    <h3 className="text-xs font-semibold mb-3" style={{ color: S.text2 }}>{t("设备类型分布")}</h3>
                    <div className="grid grid-cols-4 gap-2">
                      {devices.slice(0, 8).map((d) => {
                        const devPct = clsStats.total > 0 ? (d.count / clsStats.total * 100).toFixed(1) : "0";
                        return (
                          <button key={d.device_type}
                            onClick={() => setSelectedDevice(d.device_type)}
                            className="rounded-lg px-3 py-2 text-left transition-colors"
                            style={{ background: S.overlay, border: `1px solid ${S.border}` }}
                            onMouseEnter={(e) => (e.currentTarget.style.borderColor = S.accent)}
                            onMouseLeave={(e) => (e.currentTarget.style.borderColor = S.border)}>
                            <p className="text-xs font-medium truncate" style={{ color: S.text1 }}>{d.device_type}</p>
                            <p className="text-lg font-bold tabular-nums mt-0.5" style={{ color: S.accent }}>{d.count}</p>
                            <p className="text-[10px] font-mono" style={{ color: S.text3 }}>{devPct}%</p>
                          </button>
                        );
                      })}
                    </div>
                  </div>
                )}
              </section>
            );
          })()}

          {/* Top users + fail reasons */}
          <div className="grid grid-cols-2 gap-4 j-rise" style={{ ["--d" as string]: "0.2s" }}>
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
            <section className="rounded-xl p-5 j-rise" style={{ background: S.surface, border: `1px solid ${S.border}`, ["--d" as string]: "0.24s" }}>
              <h2 className="mb-4 text-sm font-semibold" style={{ color: S.text1 }}>{t("规则准确率")}</h2>
              <div className="overflow-hidden rounded-lg" style={{ border: `1px solid ${S.border}` }}>
                <table className="min-w-full">
                  <thead>
                    <tr style={{ background: "var(--j-hover)" }}>
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

export default function AnalyticsPage() {
  return (
    <Suspense fallback={
      <div className="flex items-center justify-center py-24">
        <div className="h-8 w-8 animate-spin rounded-full border-4"
          style={{ borderColor: "var(--j-border)", borderTopColor: "var(--j-accent)" }} />
      </div>
    }>
      <AnalyticsPageInner />
    </Suspense>
  );
}
