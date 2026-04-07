"use client";

import { useEffect, useState } from "react";
import { useT } from "@/lib/i18n";
import { Toast } from "@/components/Toast";
import {
  fetchEvalDatasets, fetchEvalRuns, fetchEvalRun, fetchGoldenSamples,
  createEvalDataset, startEvalRun, formatLocalTime,
  type EvalDataset, type EvalRun, type GoldenSample,
} from "@/lib/api";

const S = {
  surface: "#F8F9FA", overlay: "#FFFFFF", hover: "#EEF0F2",
  border: "rgba(0,0,0,0.08)", accent: "#B8922E", accentBg: "rgba(184,146,46,0.06)",
  text1: "#111827", text2: "#6B7280", text3: "#9CA3AF",
};


function StatusBadge({ status }: { status: string }) {
  const cfg: Record<string, { bg: string; color: string; border: string }> = {
    pending: { bg: "rgba(0,0,0,0.04)", color: S.text3, border: S.border },
    running: { bg: "rgba(96,165,250,0.12)", color: "#2563EB", border: "rgba(96,165,250,0.25)" },
    done: { bg: "rgba(34,197,94,0.12)", color: "#16A34A", border: "rgba(34,197,94,0.25)" },
    failed: { bg: "rgba(239,68,68,0.12)", color: "#DC2626", border: "rgba(239,68,68,0.25)" },
  };
  const s = cfg[status] || cfg.pending;
  return (
    <span className="inline-flex rounded-full px-2 py-0.5 text-[10px] font-medium"
      style={{ background: s.bg, color: s.color, border: `1px solid ${s.border}` }}>
      {status}
    </span>
  );
}

function ScoreBar({ value, label, color }: { value: number; label: string; color: string }) {
  const pct = Math.round(value * 100);
  return (
    <div className="flex items-center gap-3">
      <span className="w-24 text-xs flex-shrink-0" style={{ color: S.text2 }}>{label}</span>
      <div className="flex-1 h-3 rounded-full overflow-hidden" style={{ background: S.hover }}>
        <div className="h-full rounded-full transition-all duration-500" style={{ width: `${pct}%`, background: color }} />
      </div>
      <span className="w-12 text-right text-xs font-mono font-semibold" style={{ color }}>{pct}%</span>
    </div>
  );
}

export default function EvalPage() {
  const t = useT();
  const [tab, setTab] = useState<"datasets" | "runs">("datasets");
  const [datasets, setDatasets] = useState<EvalDataset[]>([]);
  const [runs, setRuns] = useState<EvalRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [toast, setToast] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [detailRun, setDetailRun] = useState<EvalRun | null>(null);

  const loadData = async () => {
    setLoading(true);
    try {
      const [ds, rs] = await Promise.all([fetchEvalDatasets(), fetchEvalRuns()]);
      setDatasets(ds);
      setRuns(rs);
    } catch {} finally { setLoading(false); }
  };

  useEffect(() => { loadData(); }, []);

  const handleStartEval = async (datasetId: number) => {
    try {
      const username = localStorage.getItem("appllo_username") || "";
      await startEvalRun(datasetId, {}, username);
      setToast(t("评测中..."));
      setTimeout(loadData, 2000);
    } catch { setToast(t("评测失败")); }
  };

  const viewRunDetail = async (runId: number) => {
    try {
      const run = await fetchEvalRun(runId);
      setDetailRun(run);
    } catch {}
  };

  return (
    <div className="min-h-full">
      <header className="sticky top-0 z-10 backdrop-blur-md"
        style={{ background: "rgba(255,255,255,0.92)", borderBottom: `1px solid ${S.border}` }}>
        <div className="flex items-center justify-between px-6 py-3">
          <div>
            <h1 className="text-base font-semibold" style={{ color: S.text1 }}>{t("评测中心")}</h1>
            <p className="text-xs mt-0.5" style={{ color: S.text3 }}>{t("运行评测以量化分析质量")}</p>
          </div>
          <div className="flex items-center gap-2">
            <div className="flex items-center gap-1 rounded-lg p-1" style={{ background: S.overlay }}>
              {(["datasets", "runs"] as const).map((k) => (
                <button key={k} onClick={() => setTab(k)}
                  className="rounded-md px-3 py-1.5 text-sm font-medium transition-all"
                  style={tab === k ? { background: S.surface, color: S.text1, boxShadow: "0 1px 3px rgba(0,0,0,0.1)" } : { color: S.text3 }}>
                  {k === "datasets" ? t("评测数据集") : t("评测记录")}
                </button>
              ))}
            </div>
            {tab === "datasets" && (
              <button onClick={() => setShowCreate(true)}
                className="rounded-lg px-3 py-1.5 text-sm font-semibold"
                style={{ background: S.accent, color: "#0A0B0E" }}>
                + {t("创建数据集")}
              </button>
            )}
          </div>
        </div>
      </header>

      <div className="mx-auto max-w-4xl px-6 py-6 space-y-4">
        {loading ? (
          <div className="flex items-center justify-center py-24">
            <div className="h-8 w-8 animate-spin rounded-full border-4"
              style={{ borderColor: "rgba(0,0,0,0.08)", borderTopColor: S.accent }} />
          </div>
        ) : tab === "datasets" ? (
          datasets.length === 0 ? (
            <p className="py-24 text-center text-sm" style={{ color: S.text3 }}>{t("暂无评测数据集")}</p>
          ) : (
            datasets.map((ds) => (
              <div key={ds.id} className="rounded-xl px-5 py-4"
                style={{ background: S.overlay, border: `1px solid ${S.border}` }}>
                <div className="flex items-center justify-between">
                  <div>
                    <h3 className="text-sm font-semibold" style={{ color: S.text1 }}>{ds.name}</h3>
                    {ds.description && <p className="text-xs mt-0.5" style={{ color: S.text2 }}>{ds.description}</p>}
                    <p className="text-[10px] mt-1 font-mono" style={{ color: S.text3 }}>
                      {ds.sample_ids.length} {t("样本数")} | {formatLocalTime(ds.created_at)}
                    </p>
                  </div>
                  <button onClick={() => handleStartEval(ds.id)}
                    className="rounded-lg px-4 py-2 text-sm font-semibold"
                    style={{ background: S.accentBg, color: S.accent, border: "1px solid rgba(184,146,46,0.3)" }}>
                    {t("开始评测")}
                  </button>
                </div>
              </div>
            ))
          )
        ) : (
          runs.length === 0 ? (
            <p className="py-24 text-center text-sm" style={{ color: S.text3 }}>{t("暂无评测记录")}</p>
          ) : (
            runs.map((run) => (
              <div key={run.id} className="rounded-xl px-5 py-4 cursor-pointer transition-colors"
                style={{ background: S.overlay, border: `1px solid ${S.border}` }}
                onClick={() => viewRunDetail(run.id)}
                onMouseEnter={(e) => (e.currentTarget.style.background = S.surface)}
                onMouseLeave={(e) => (e.currentTarget.style.background = S.overlay)}>
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-semibold" style={{ color: S.text1 }}>
                      Run #{run.id}
                    </span>
                    <StatusBadge status={run.status} />
                  </div>
                  <span className="text-[10px] font-mono" style={{ color: S.text3 }}>
                    {formatLocalTime(run.created_at)}
                  </span>
                </div>
                {run.status === "done" && run.summary?.avg_overall_score !== undefined && (
                  <div className="space-y-1.5">
                    <ScoreBar value={run.summary.avg_overall_score} label={t("综合评分")} color={S.accent} />
                    <ScoreBar value={run.summary.avg_problem_type_match} label={t("类型匹配")} color="#2563EB" />
                    <ScoreBar value={run.summary.avg_root_cause_similarity} label={t("根因相似度")} color="#7C3AED" />
                    <ScoreBar value={run.summary.avg_confidence_match} label={t("置信度匹配")} color="#16A34A" />
                  </div>
                )}
                {run.status === "failed" && run.summary?.error && (
                  <p className="text-xs mt-1" style={{ color: "#DC2626" }}>{run.summary.error}</p>
                )}
              </div>
            ))
          )
        )}
      </div>

      {/* Create dataset dialog */}
      {showCreate && <CreateDatasetDialog t={t} onClose={() => setShowCreate(false)} onCreated={() => { setShowCreate(false); loadData(); setToast(t("已保存")); }} />}

      {/* Run detail panel */}
      {detailRun && (
        <div className="fixed inset-0 z-50 flex">
          <div className="flex-1 backdrop-blur-sm" style={{ background: "rgba(0,0,0,0.65)" }} onClick={() => setDetailRun(null)} />
          <div className="w-[560px] flex-shrink-0 overflow-y-auto" style={{ background: "#FFFFFF", borderLeft: `1px solid ${S.border}` }}>
            <div className="sticky top-0 z-10 flex items-center justify-between px-5 py-3"
              style={{ background: "#FFFFFF", borderBottom: `1px solid ${S.border}` }}>
              <h2 className="text-sm font-semibold" style={{ color: S.text1 }}>{t("评测详情")} — Run #{detailRun.id}</h2>
              <button onClick={() => setDetailRun(null)} className="rounded-lg p-1.5" style={{ color: S.text3 }}>
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
            <div className="p-5 space-y-4">
              {detailRun.summary?.avg_overall_score !== undefined && (
                <section className="rounded-xl p-4" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
                  <p className="text-xs mb-3" style={{ color: S.text3 }}>{t("总分")}</p>
                  <div className="space-y-2">
                    <ScoreBar value={detailRun.summary.avg_overall_score} label={t("综合评分")} color={S.accent} />
                    <ScoreBar value={detailRun.summary.avg_problem_type_match} label={t("类型匹配")} color="#2563EB" />
                    <ScoreBar value={detailRun.summary.avg_root_cause_similarity} label={t("根因相似度")} color="#7C3AED" />
                    <ScoreBar value={detailRun.summary.avg_confidence_match} label={t("置信度匹配")} color="#16A34A" />
                  </div>
                  <p className="mt-3 text-[11px] font-mono" style={{ color: S.text3 }}>
                    {detailRun.summary.completed}/{detailRun.summary.total_samples} completed
                    {detailRun.summary.errors > 0 && `, ${detailRun.summary.errors} errors`}
                  </p>
                </section>
              )}

              <h3 className="text-xs font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>{t("评测详情")}</h3>
              {(detailRun.results || []).map((r: any, idx: number) => (
                <div key={idx} className="rounded-xl p-4" style={{ background: S.overlay, border: `1px solid ${S.border}` }}>
                  <div className="flex items-center justify-between mb-2">
                    <span className="text-xs font-mono" style={{ color: S.text3 }}>Sample #{r.sample_id}</span>
                    {r.status === "ok" ? (
                      <span className="text-xs font-bold tabular-nums" style={{ color: r.scores.overall_score >= 0.7 ? "#16A34A" : r.scores.overall_score >= 0.4 ? "#EA580C" : "#DC2626" }}>
                        {Math.round(r.scores.overall_score * 100)}%
                      </span>
                    ) : (
                      <span className="text-xs" style={{ color: "#DC2626" }}>error</span>
                    )}
                  </div>
                  {r.status === "ok" && (
                    <div className="grid grid-cols-2 gap-3 text-xs">
                      <div>
                        <p className="text-[10px] font-semibold uppercase mb-1" style={{ color: S.text3 }}>{t("预期结果")}</p>
                        <p style={{ color: S.text2 }}>{r.golden?.problem_type}</p>
                        <p className="mt-1" style={{ color: S.text3, display: "-webkit-box", WebkitLineClamp: 3, WebkitBoxOrient: "vertical", overflow: "hidden" }}>
                          {r.golden?.root_cause}
                        </p>
                      </div>
                      <div>
                        <p className="text-[10px] font-semibold uppercase mb-1" style={{ color: S.text3 }}>{t("实际结果")}</p>
                        <p style={{ color: S.text2 }}>{r.actual?.problem_type}</p>
                        <p className="mt-1" style={{ color: S.text3, display: "-webkit-box", WebkitLineClamp: 3, WebkitBoxOrient: "vertical", overflow: "hidden" }}>
                          {r.actual?.root_cause}
                        </p>
                      </div>
                    </div>
                  )}
                  {r.status === "error" && (
                    <p className="text-xs" style={{ color: "#DC2626" }}>{r.error}</p>
                  )}
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {toast && <Toast msg={toast} onClose={() => setToast("")} />}
    </div>
  );
}

function CreateDatasetDialog({ t, onClose, onCreated }: { t: (k: string) => string; onClose: () => void; onCreated: () => void }) {
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [samples, setSamples] = useState<GoldenSample[]>([]);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchGoldenSamples().then((s) => { setSamples(s); setLoading(false); }).catch(() => setLoading(false));
  }, []);

  const toggle = (id: number) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  };

  const handleCreate = async () => {
    if (!name.trim() || selectedIds.size === 0) return;
    const username = localStorage.getItem("appllo_username") || "";
    await createEvalDataset({ name, description: desc, sample_ids: Array.from(selectedIds), created_by: username });
    onCreated();
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ background: "rgba(0,0,0,0.5)" }}>
      <div className="w-[560px] max-h-[80vh] overflow-y-auto rounded-2xl p-6"
        style={{ background: "#FFFFFF", border: `1px solid ${S.border}` }}>
        <h2 className="text-base font-semibold mb-4" style={{ color: S.text1 }}>{t("创建数据集")}</h2>

        <div className="space-y-3 mb-4">
          <div>
            <label className="block text-xs font-medium mb-1" style={{ color: S.text3 }}>{t("数据集名称")}</label>
            <input value={name} onChange={(e) => setName(e.target.value)}
              className="w-full rounded-lg px-3 py-2 text-sm outline-none"
              style={{ background: S.surface, border: `1px solid ${S.border}`, color: S.text1 }} />
          </div>
          <div>
            <label className="block text-xs font-medium mb-1" style={{ color: S.text3 }}>Description</label>
            <input value={desc} onChange={(e) => setDesc(e.target.value)}
              className="w-full rounded-lg px-3 py-2 text-sm outline-none"
              style={{ background: S.surface, border: `1px solid ${S.border}`, color: S.text1 }} />
          </div>
        </div>

        <div className="mb-4">
          <label className="block text-xs font-medium mb-2" style={{ color: S.text3 }}>{t("选择样本")} ({selectedIds.size} {t("已选")})</label>
          {loading ? (
            <p className="text-xs py-4 text-center" style={{ color: S.text3 }}>{t("加载中...")}</p>
          ) : (
            <div className="max-h-64 overflow-y-auto rounded-lg" style={{ border: `1px solid ${S.border}` }}>
              {samples.map((s) => (
                <label key={s.id}
                  className="flex items-center gap-3 px-3 py-2 cursor-pointer transition-colors"
                  style={{ borderBottom: `1px solid ${S.border}` }}
                  onMouseEnter={(e) => (e.currentTarget.style.background = S.hover)}
                  onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
                >
                  <input type="checkbox" checked={selectedIds.has(s.id)} onChange={() => toggle(s.id)}
                    className="accent-amber-600" />
                  <div className="flex-1 min-w-0">
                    <p className="text-xs font-medium" style={{ color: S.text1 }}>{s.problem_type}</p>
                    <p className="text-[10px] truncate" style={{ color: S.text3 }}>{s.description}</p>
                  </div>
                  {s.rule_type && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded" style={{ background: S.accentBg, color: S.accent }}>{s.rule_type}</span>
                  )}
                </label>
              ))}
            </div>
          )}
        </div>

        <div className="flex justify-end gap-2">
          <button onClick={onClose}
            className="rounded-lg px-4 py-2 text-sm font-medium"
            style={{ border: `1px solid ${S.border}`, color: S.text2 }}>
            {t("取消")}
          </button>
          <button onClick={handleCreate}
            disabled={!name.trim() || selectedIds.size === 0}
            className="rounded-lg px-4 py-2 text-sm font-semibold disabled:opacity-40"
            style={{ background: S.accent, color: "#0A0B0E" }}>
            {t("创建")}
          </button>
        </div>
      </div>
    </div>
  );
}
