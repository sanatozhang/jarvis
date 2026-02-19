"use client";

import { useT } from "@/lib/i18n";

import { useEffect, useState } from "react";

interface Analytics {
  date_from: string;
  date_to: string;
  event_counts: Record<string, number>;
  unique_users: number;
  avg_analysis_duration_ms: number;
  avg_analysis_duration_min: number;
  total_analyses: number;
  successful_analyses: number;
  failed_analyses: number;
  feedback_submitted: number;
  escalations: number;
  fail_reasons: { reason?: string; error?: string }[];
  daily: Record<string, Record<string, number>>;
  top_users: { username: string; count: number }[];
  value_metrics: {
    time_saved_hours: number;
    time_saved_per_ticket_min: number;
    success_rate: number;
    estimated_manual_hours: number;
    estimated_ai_hours: number;
  };
}

function StatCard({ label, value, sub, color }: { label: string; value: string | number; sub?: string; color?: string }) {
  return (
    <div className="rounded-xl border border-gray-100 bg-white px-4 py-4">
      <p className="text-xs text-gray-400">{label}</p>
      <p className={`mt-1 text-2xl font-bold ${color || ""}`}>{value}</p>
      {sub && <p className="mt-0.5 text-[11px] text-gray-400">{sub}</p>}
    </div>
  );
}

function Bar({ value, max, color }: { value: number; max: number; color: string }) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  return (
    <div className="h-5 w-full overflow-hidden rounded-full bg-gray-100">
      <div className={`h-full rounded-full ${color} transition-all duration-500`} style={{ width: `${pct}%` }} />
    </div>
  );
}

export default function AnalyticsPage() {
  const [data, setData] = useState<Analytics | null>(null);
  const [days, setDays] = useState(7);
  const [loading, setLoading] = useState(true);

  const load = async (d: number) => {
    setLoading(true);
    try {
      const res = await fetch(`/api/analytics/dashboard?days=${d}`);
      if (res.ok) setData(await res.json());
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
      <header className="sticky top-0 z-10 border-b border-gray-200 bg-white/80 backdrop-blur-sm">
        <div className="flex items-center justify-between px-6 py-3">
          <div>
            <h1 className="text-lg font-semibold">数据看板</h1>
            <p className="text-xs text-gray-400">项目价值 & 使用情况统计</p>
          </div>
          <div className="flex items-center gap-1 rounded-lg bg-gray-100 p-1">
            {[7, 14, 30].map((d) => (
              <button key={d} onClick={() => setDays(d)} className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${days === d ? "bg-white text-gray-900 shadow-sm" : "text-gray-500"}`}>
                {d}天
              </button>
            ))}
          </div>
        </div>
      </header>

      {loading && !data ? (
        <div className="flex items-center justify-center py-24">
          <div className="h-8 w-8 animate-spin rounded-full border-4 border-gray-200 border-t-black" />
        </div>
      ) : !data ? (
        <p className="py-24 text-center text-gray-300">暂无数据</p>
      ) : (
        <div className="mx-auto max-w-4xl px-6 py-6 space-y-6">

          {/* ===== VALUE METRICS (HERO) ===== */}
          <section className="rounded-2xl bg-gradient-to-br from-black to-gray-800 p-6 text-white">
            <h2 className="mb-4 text-sm font-medium text-white/60">项目价值（过去 {days} 天）</h2>
            <div className="grid grid-cols-3 gap-4">
              <div>
                <p className="text-3xl font-bold">{data.value_metrics.time_saved_hours}<span className="text-lg font-normal text-white/50"> 小时</span></p>
                <p className="mt-1 text-xs text-white/50">预估节省工时</p>
              </div>
              <div>
                <p className="text-3xl font-bold">{data.value_metrics.time_saved_per_ticket_min}<span className="text-lg font-normal text-white/50"> 分钟/单</span></p>
                <p className="mt-1 text-xs text-white/50">每单节省时间</p>
              </div>
              <div>
                <p className="text-3xl font-bold">{data.value_metrics.success_rate}<span className="text-lg font-normal text-white/50">%</span></p>
                <p className="mt-1 text-xs text-white/50">分析成功率</p>
              </div>
            </div>
            <p className="mt-3 text-[11px] text-white/30">
              对比: 人工处理 ~{data.value_metrics.estimated_manual_hours}h → AI 处理 ~{data.value_metrics.estimated_ai_hours}h
            </p>
          </section>

          {/* ===== KEY METRICS ===== */}
          <div className="grid grid-cols-5 gap-3">
            <StatCard label="总分析次数" value={data.total_analyses} />
            <StatCard label="分析成功" value={data.successful_analyses} color="text-green-600" />
            <StatCard label="分析失败" value={data.failed_analyses} color="text-red-600" />
            <StatCard label="反馈提交" value={data.feedback_submitted} color="text-blue-600" />
            <StatCard label="活跃用户" value={data.unique_users} color="text-purple-600" />
          </div>

          <div className="grid grid-cols-3 gap-3">
            <StatCard label="平均分析耗时" value={`${data.avg_analysis_duration_min} 分钟`} sub={`${data.avg_analysis_duration_ms}ms`} />
            <StatCard label="工单转工程师" value={data.escalations} />
            <StatCard label="页面访问" value={data.event_counts.page_visit || 0} />
          </div>

          {/* ===== DAILY TREND ===== */}
          <section className="rounded-xl border border-gray-100 bg-white p-5">
            <h2 className="mb-4 text-sm font-semibold">每日趋势</h2>
            {dailyDates.length === 0 ? (
              <p className="py-8 text-center text-sm text-gray-300">暂无数据</p>
            ) : (
              <div className="space-y-2">
                {dailyDates.map((date) => {
                  const day = data.daily[date];
                  const analyses = (day.analysis_start || 0);
                  const success = (day.analysis_done || 0);
                  const fail = (day.analysis_fail || 0);
                  const feedback = (day.feedback_submit || 0);
                  return (
                    <div key={date} className="flex items-center gap-3">
                      <span className="w-20 flex-shrink-0 text-xs font-mono text-gray-400">{date.slice(5)}</span>
                      <div className="flex-1">
                        <Bar value={analyses + feedback} max={maxDaily} color="bg-black" />
                      </div>
                      <div className="flex w-40 flex-shrink-0 items-center gap-2 text-[11px]">
                        <span className="text-gray-600">{analyses} 分析</span>
                        <span className="text-green-600">{success} 成功</span>
                        <span className="text-red-500">{fail} 失败</span>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </section>

          {/* ===== TOP USERS + FAIL REASONS ===== */}
          <div className="grid grid-cols-2 gap-4">
            <section className="rounded-xl border border-gray-100 bg-white p-5">
              <h2 className="mb-3 text-sm font-semibold">活跃用户 Top 10</h2>
              {data.top_users.length === 0 ? (
                <p className="py-4 text-center text-sm text-gray-300">暂无数据</p>
              ) : (
                <div className="space-y-2">
                  {data.top_users.map((u, i) => (
                    <div key={u.username} className="flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <span className="flex h-5 w-5 items-center justify-center rounded-full bg-gray-100 text-[10px] font-bold text-gray-500">{i + 1}</span>
                        <span className="text-sm text-gray-700">{u.username}</span>
                      </div>
                      <span className="text-xs tabular-nums text-gray-400">{u.count} 次操作</span>
                    </div>
                  ))}
                </div>
              )}
            </section>

            <section className="rounded-xl border border-gray-100 bg-white p-5">
              <h2 className="mb-3 text-sm font-semibold">失败原因分布</h2>
              {data.fail_reasons.length === 0 ? (
                <p className="py-4 text-center text-sm text-gray-300">暂无失败记录</p>
              ) : (() => {
                const reasonCounts: Record<string, number> = {};
                data.fail_reasons.forEach((f) => {
                  const r = f.reason || "未知";
                  reasonCounts[r] = (reasonCounts[r] || 0) + 1;
                });
                return (
                  <div className="space-y-2">
                    {Object.entries(reasonCounts).sort((a, b) => b[1] - a[1]).map(([reason, count]) => (
                      <div key={reason} className="flex items-center justify-between">
                        <span className="text-sm text-gray-600">{reason}</span>
                        <span className="rounded-full bg-red-50 px-2 py-0.5 text-[11px] font-medium text-red-600">{count}</span>
                      </div>
                    ))}
                  </div>
                );
              })()}
            </section>
          </div>
        </div>
      )}
    </div>
  );
}
