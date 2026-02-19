"use client";

import { useT } from "@/lib/i18n";

import { useEffect, useState } from "react";
import { fetchDailyReport, fetchReportDates, type DailyReport } from "@/lib/api";

export default function ReportsPage() {
  const [dates, setDates] = useState<string[]>([]);
  const [selectedDate, setSelectedDate] = useState("");
  const [report, setReport] = useState<DailyReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [toast, setToast] = useState("");

  useEffect(() => {
    fetchReportDates().then((r) => {
      setDates(r.dates);
      if (r.dates.length > 0) setSelectedDate(r.dates[0]);
    }).catch(console.error);
  }, []);

  useEffect(() => {
    if (!selectedDate) return;
    setLoading(true);
    fetchDailyReport(selectedDate)
      .then(setReport)
      .catch(console.error)
      .finally(() => setLoading(false));
  }, [selectedDate]);

  const today = new Date().toISOString().slice(0, 10);

  const copyMd = () => {
    if (!report) return;
    navigator.clipboard.writeText(report.markdown);
    setToast("Markdown 已复制");
    setTimeout(() => setToast(""), 2000);
  };

  return (
    <div className="min-h-full">
      <header className="sticky top-0 z-10 border-b border-gray-200 bg-white/80 backdrop-blur-sm">
        <div className="flex items-center justify-between px-6 py-3">
          <h1 className="text-lg font-semibold">值班报告</h1>
          <div className="flex items-center gap-2">
            <select
              value={selectedDate}
              onChange={(e) => setSelectedDate(e.target.value)}
              className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm text-gray-700 outline-none focus:border-black"
            >
              {/* Always show today */}
              {!dates.includes(today) && <option value={today}>{today}（今天）</option>}
              {dates.map((d) => (
                <option key={d} value={d}>{d}{d === today ? "（今天）" : ""}</option>
              ))}
              {dates.length === 0 && <option value="">暂无报告</option>}
            </select>
            {report && (
              <button onClick={copyMd} className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm font-medium text-gray-600 hover:bg-gray-50">
                复制 Markdown
              </button>
            )}
          </div>
        </div>
      </header>

      <div className="px-6 py-5">
        {loading ? (
          <p className="py-16 text-center text-sm text-gray-300">加载报告中...</p>
        ) : !report ? (
          <p className="py-16 text-center text-sm text-gray-300">选择日期查看报告</p>
        ) : report.total_issues === 0 ? (
          <div className="py-16 text-center">
            <p className="text-sm text-gray-400">该日期暂无已分析工单</p>
            <p className="mt-1 text-xs text-gray-300">分析工单后，报告会自动生成</p>
          </div>
        ) : (
          <>
            {/* Summary cards */}
            <div className="mb-5 grid grid-cols-4 gap-3">
              <div className="rounded-xl border border-gray-100 bg-white px-4 py-3">
                <p className="text-xs text-gray-400">总工单数</p>
                <p className="mt-0.5 text-xl font-bold">{report.total_issues}</p>
              </div>
              {Object.entries(report.category_stats).slice(0, 3).map(([cat, count]) => (
                <div key={cat} className="rounded-xl border border-gray-100 bg-white px-4 py-3">
                  <p className="text-xs text-gray-400">{cat}</p>
                  <p className="mt-0.5 text-xl font-bold">{count}</p>
                </div>
              ))}
            </div>

            {/* Analysis list */}
            <div className="space-y-3">
              {report.analyses.map((a: any, i: number) => (
                <div key={i} className="rounded-xl border border-gray-100 bg-white p-4">
                  <div className="mb-2 flex items-start justify-between">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-semibold">#{i + 1}</span>
                      <span className="rounded bg-gray-100 px-2 py-0.5 text-xs font-medium text-gray-600">{a.problem_type}</span>
                      <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${
                        a.confidence === "high" ? "bg-green-50 text-green-600" :
                        a.confidence === "medium" ? "bg-yellow-50 text-yellow-600" :
                        "bg-red-50 text-red-600"
                      }`}>{a.confidence}</span>
                      {a.needs_engineer && <span className="rounded bg-amber-50 px-2 py-0.5 text-[10px] font-medium text-amber-600">需工程师</span>}
                    </div>
                    <span className="font-mono text-[11px] text-gray-400">{a.issue_id.slice(0, 12)}</span>
                  </div>
                  <p className="text-sm text-gray-600">{a.root_cause}</p>
                  {a.user_reply && (
                    <div className="mt-3 rounded-lg bg-green-50/50 p-3">
                      <div className="mb-1 flex items-center justify-between">
                        <span className="text-[10px] font-semibold uppercase tracking-wider text-gray-400">用户回复</span>
                        <button
                          onClick={() => { navigator.clipboard.writeText(a.user_reply); setToast("已复制"); setTimeout(() => setToast(""), 2000); }}
                          className="rounded bg-green-600 px-2 py-0.5 text-[10px] font-medium text-white hover:bg-green-700"
                        >
                          复制
                        </button>
                      </div>
                      <p className="whitespace-pre-wrap text-sm text-gray-600">{a.user_reply}</p>
                    </div>
                  )}
                </div>
              ))}
            </div>

            {/* Raw Markdown */}
            <details className="mt-6">
              <summary className="cursor-pointer text-sm font-medium text-gray-400 hover:text-gray-600">
                查看原始 Markdown
              </summary>
              <pre className="mt-2 max-h-96 overflow-y-auto rounded-lg bg-gray-50 p-4 font-mono text-xs text-gray-500">
                {report.markdown}
              </pre>
            </details>
          </>
        )}
      </div>

      {toast && (
        <div className="fixed bottom-6 right-6 z-50 rounded-lg bg-gray-900 px-4 py-2.5 text-sm font-medium text-white shadow-lg">
          {toast}
        </div>
      )}
    </div>
  );
}
