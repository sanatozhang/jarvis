"use client";

import { useT } from "@/lib/i18n";
import { useEffect, useState } from "react";
import { fetchDailyReport, fetchReportDates, type DailyReport } from "@/lib/api";
import MarkdownText from "@/components/MarkdownText";

const S = {
  surface: "#F8F9FA", overlay: "#FFFFFF", hover: "#EEF0F2",
  border: "rgba(0,0,0,0.08)", borderSm: "rgba(0,0,0,0.04)",
  accent: "#B8922E", accentBg: "rgba(184,146,46,0.06)",
  text1: "#111827", text2: "#6B7280", text3: "#9CA3AF",
};

function Toast({ msg, onClose }: { msg: string; onClose: () => void }) {
  useEffect(() => { const id = setTimeout(onClose, 2500); return () => clearTimeout(id); }, [onClose]);
  return (
    <div className="fixed bottom-6 right-6 z-50 rounded-xl px-4 py-2.5 text-sm font-medium shadow-2xl"
      style={{ background: S.surface, color: S.text1, border: `1px solid ${S.border}` }}>
      {msg}
    </div>
  );
}

function ConfBadge({ conf }: { conf: string }) {
  const styles: Record<string, { bg: string; color: string; border: string }> = {
    high: { bg: "rgba(34,197,94,0.12)", color: "#16A34A", border: "1px solid rgba(34,197,94,0.25)" },
    medium: { bg: "rgba(234,179,8,0.12)", color: "#CA8A04", border: "1px solid rgba(234,179,8,0.25)" },
    low: { bg: "rgba(239,68,68,0.12)", color: "#DC2626", border: "1px solid rgba(239,68,68,0.25)" },
  };
  const s = styles[conf] || styles.low;
  return (
    <span className="rounded-full px-2 py-0.5 text-[10px] font-semibold" style={s}>{conf}</span>
  );
}

export default function ReportsPage() {
  const t = useT();
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
    setToast(t("Markdown 已复制"));
    setTimeout(() => setToast(""), 2000);
  };

  return (
    <div className="min-h-full">
      {/* Header */}
      <header className="sticky top-0 z-10 backdrop-blur-md"
        style={{ background: "rgba(255,255,255,0.92)", borderBottom: `1px solid ${S.border}` }}>
        <div className="flex items-center justify-between px-6 py-3">
          <div>
            <h1 className="text-base font-semibold" style={{ color: S.text1 }}>{t("值班报告")}</h1>
            <p className="text-xs mt-0.5" style={{ color: S.text3 }}>{t("每日分析汇总与用户回复模板")}</p>
          </div>
          <div className="flex items-center gap-2">
            <select
              value={selectedDate}
              onChange={(e) => setSelectedDate(e.target.value)}
              className="rounded-lg px-3 py-1.5 text-sm outline-none font-sans"
              style={{ background: S.overlay, border: `1px solid ${S.border}`, color: S.text1 }}>
              {!dates.includes(today) && (
                <option value={today}>{today}（{t("今天")}）</option>
              )}
              {dates.map((d) => (
                <option key={d} value={d}>{d}{d === today ? `（${t("今天")}）` : ""}</option>
              ))}
              {dates.length === 0 && <option value="">{t("暂无报告")}</option>}
            </select>
            {report && (
              <button onClick={copyMd}
                className="rounded-lg px-3 py-1.5 text-sm font-medium transition-colors"
                style={{ border: `1px solid ${S.border}`, color: S.text2 }}>
                {t("复制 Markdown")}
              </button>
            )}
          </div>
        </div>
      </header>

      <div className="px-6 py-5">
        {loading ? (
          <div className="flex items-center justify-center py-24">
            <div className="h-8 w-8 animate-spin rounded-full border-4"
              style={{ borderColor: "rgba(0,0,0,0.08)", borderTopColor: S.accent }} />
          </div>
        ) : !report ? (
          <div className="flex items-center justify-center py-24">
            <p className="text-sm" style={{ color: S.text3 }}>{t("选择日期查看报告")}</p>
          </div>
        ) : report.total_issues === 0 ? (
          <div className="py-24 text-center">
            <p className="text-sm" style={{ color: S.text3 }}>{t("该日期暂无已分析工单")}</p>
            <p className="mt-1 text-xs" style={{ color: S.text3 }}>{t("分析工单后，报告会自动生成")}</p>
          </div>
        ) : (
          <div className="mx-auto max-w-3xl space-y-5">
            {/* Summary cards */}
            <div className="grid grid-cols-4 gap-3">
              <div className="rounded-xl px-4 py-3" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
                <p className="text-[10px] font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>{t("总工单数")}</p>
                <p className="mt-1 text-2xl font-bold tabular-nums" style={{ color: S.text1 }}>{report.total_issues}</p>
              </div>
              {Object.entries(report.category_stats).slice(0, 3).map(([cat, count]) => (
                <div key={cat} className="rounded-xl px-4 py-3" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
                  <p className="text-[10px] font-semibold uppercase tracking-wider truncate" style={{ color: S.text3 }}>{cat}</p>
                  <p className="mt-1 text-2xl font-bold tabular-nums" style={{ color: S.accent }}>{count as number}</p>
                </div>
              ))}
            </div>

            {/* Analysis list */}
            <div className="space-y-3">
              {report.analyses.map((a: any, i: number) => (
                <div key={i} className="rounded-xl p-4" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
                  <div className="mb-3 flex items-start justify-between">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-sm font-bold tabular-nums" style={{ color: S.text3 }}>#{i + 1}</span>
                      <span className="rounded-md px-2 py-0.5 text-xs font-medium"
                        style={{ background: S.overlay, color: S.text2, border: `1px solid ${S.border}` }}>
                        {a.problem_type}
                      </span>
                      <ConfBadge conf={a.confidence} />
                      {a.needs_engineer && (
                        <span className="rounded-md px-2 py-0.5 text-[10px] font-semibold"
                          style={{ background: S.accentBg, color: S.accent, border: "1px solid rgba(184,146,46,0.25)" }}>
                          {t("需工程师")}
                        </span>
                      )}
                    </div>
                    <span className="font-mono text-[11px] flex-shrink-0" style={{ color: S.text3 }}>
                      {a.issue_id.slice(0, 12)}
                    </span>
                  </div>

                  <div className="text-sm leading-relaxed" style={{ color: S.text2 }}><MarkdownText>{a.root_cause}</MarkdownText></div>

                  {a.user_reply && (
                    <div className="mt-3 rounded-lg p-3"
                      style={{ background: "rgba(34,197,94,0.06)", border: "1px solid rgba(34,197,94,0.15)" }}>
                      <div className="mb-2 flex items-center justify-between">
                        <span className="text-[10px] font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>
                          {t("用户回复")}
                        </span>
                        <button
                          onClick={() => {
                            navigator.clipboard.writeText(a.user_reply);
                            setToast(t("已复制"));
                            setTimeout(() => setToast(""), 2000);
                          }}
                          className="rounded-md px-2 py-0.5 text-[10px] font-semibold transition-opacity hover:opacity-80"
                          style={{ background: "rgba(34,197,94,0.15)", color: "#16A34A", border: "1px solid rgba(34,197,94,0.3)" }}>
                          {t("复制")}
                        </button>
                      </div>
                      <div className="text-sm leading-relaxed" style={{ color: S.text2 }}><MarkdownText>{a.user_reply}</MarkdownText></div>
                    </div>
                  )}
                </div>
              ))}
            </div>

            {/* Raw Markdown */}
            <details className="rounded-xl overflow-hidden" style={{ border: `1px solid ${S.border}` }}>
              <summary className="cursor-pointer px-5 py-3 text-sm font-medium select-none"
                style={{ color: S.text3, background: S.surface }}
                onMouseEnter={(e) => (e.currentTarget.style.color = S.text2)}
                onMouseLeave={(e) => (e.currentTarget.style.color = S.text3)}>
                {t("查看原始 Markdown")}
              </summary>
              <pre className="max-h-96 overflow-y-auto p-5 font-mono text-xs leading-relaxed"
                style={{ background: S.overlay, color: S.text3 }}>
                {report.markdown}
              </pre>
            </details>
          </div>
        )}
      </div>

      {toast && <Toast msg={toast} onClose={() => setToast("")} />}
    </div>
  );
}
