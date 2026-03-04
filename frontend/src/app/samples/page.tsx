"use client";

import { useEffect, useState } from "react";
import { useT } from "@/lib/i18n";
import {
  fetchGoldenSamples,
  fetchGoldenSamplesStats,
  deleteGoldenSample,
  formatLocalTime,
  type GoldenSample,
  type GoldenSamplesStats,
} from "@/lib/api";

const S = {
  surface: "#F8F9FA", overlay: "#FFFFFF", hover: "#EEF0F2",
  border: "rgba(0,0,0,0.08)", accent: "#B8922E", accentBg: "rgba(184,146,46,0.06)",
  text1: "#111827", text2: "#6B7280", text3: "#9CA3AF",
};

function ConfBadge({ c }: { c: string }) {
  const colors: Record<string, { bg: string; color: string; border: string }> = {
    high: { bg: "rgba(34,197,94,0.12)", color: "#16A34A", border: "rgba(34,197,94,0.25)" },
    medium: { bg: "rgba(251,146,60,0.12)", color: "#EA580C", border: "rgba(251,146,60,0.25)" },
    low: { bg: "rgba(239,68,68,0.12)", color: "#DC2626", border: "rgba(239,68,68,0.25)" },
  };
  const s = colors[c] || colors.medium;
  return (
    <span className="inline-flex rounded-full px-2 py-0.5 text-[10px] font-medium"
      style={{ background: s.bg, color: s.color, border: `1px solid ${s.border}` }}>
      {c}
    </span>
  );
}

function Toast({ msg, onClose }: { msg: string; onClose: () => void }) {
  useEffect(() => { const id = setTimeout(onClose, 2500); return () => clearTimeout(id); }, [onClose]);
  return (
    <div className="fixed bottom-6 right-6 z-50 rounded-xl px-4 py-2.5 text-sm font-medium shadow-2xl"
      style={{ background: S.surface, color: S.text1, border: `1px solid ${S.border}` }}>
      {msg}
    </div>
  );
}

export default function SamplesPage() {
  const t = useT();
  const [samples, setSamples] = useState<GoldenSample[]>([]);
  const [stats, setStats] = useState<GoldenSamplesStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [filterRule, setFilterRule] = useState("");
  const [toast, setToast] = useState("");
  const [groupByRule, setGroupByRule] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const [s, st] = await Promise.all([
        fetchGoldenSamples(filterRule || undefined),
        fetchGoldenSamplesStats(),
      ]);
      setSamples(s);
      setStats(st);
    } catch {} finally { setLoading(false); }
  };

  useEffect(() => { load(); }, [filterRule]);

  const handleDelete = async (id: number) => {
    if (!confirm(t("确定要删除此金样本吗？"))) return;
    try {
      await deleteGoldenSample(id);
      setToast(t("金样本已删除"));
      load();
    } catch {}
  };

  const ruleTypes = stats ? Object.keys(stats.by_rule_type).sort() : [];
  const grouped: Record<string, GoldenSample[]> = {};
  if (groupByRule) {
    for (const s of samples) {
      const rt = s.rule_type || "general";
      if (!grouped[rt]) grouped[rt] = [];
      grouped[rt].push(s);
    }
  }

  return (
    <div className="min-h-full">
      <header className="sticky top-0 z-10 backdrop-blur-md"
        style={{ background: "rgba(255,255,255,0.92)", borderBottom: `1px solid ${S.border}` }}>
        <div className="flex items-center justify-between px-6 py-3">
          <div>
            <h1 className="text-base font-semibold" style={{ color: S.text1 }}>{t("金样本库")}</h1>
            <p className="text-xs mt-0.5" style={{ color: S.text3 }}>{t("管理已验证的准确分析样本")}</p>
          </div>
          <div className="flex items-center gap-2">
            <select
              value={filterRule}
              onChange={(e) => setFilterRule(e.target.value)}
              className="rounded-lg px-3 py-1.5 text-sm outline-none"
              style={{ background: S.overlay, border: `1px solid ${S.border}`, color: S.text1 }}
            >
              <option value="">{t("全部")}</option>
              {ruleTypes.map((rt) => <option key={rt} value={rt}>{rt}</option>)}
            </select>
            <div className="flex items-center gap-1 rounded-lg p-1" style={{ background: S.overlay }}>
              <button onClick={() => setGroupByRule(false)}
                className="rounded-md px-3 py-1.5 text-sm font-medium transition-all"
                style={!groupByRule ? { background: S.surface, color: S.text1, boxShadow: "0 1px 3px rgba(0,0,0,0.1)" } : { color: S.text3 }}>
                {t("按时间排序")}
              </button>
              <button onClick={() => setGroupByRule(true)}
                className="rounded-md px-3 py-1.5 text-sm font-medium transition-all"
                style={groupByRule ? { background: S.surface, color: S.text1, boxShadow: "0 1px 3px rgba(0,0,0,0.1)" } : { color: S.text3 }}>
                {t("按规则分组")}
              </button>
            </div>
          </div>
        </div>
      </header>

      <div className="mx-auto max-w-4xl px-6 py-6 space-y-5">
        {/* Stats */}
        {stats && (
          <div className="grid grid-cols-4 gap-3">
            <div className="rounded-xl px-4 py-3" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
              <p className="text-xs" style={{ color: S.text3 }}>{t("样本统计")}</p>
              <p className="mt-1 text-2xl font-bold" style={{ color: S.text1 }}>{stats.total}</p>
            </div>
            {Object.entries(stats.by_rule_type).slice(0, 3).map(([rt, count]) => (
              <div key={rt} className="rounded-xl px-4 py-3" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
                <p className="text-xs" style={{ color: S.text3 }}>{rt}</p>
                <p className="mt-1 text-2xl font-bold tabular-nums" style={{ color: S.accent }}>{count}</p>
              </div>
            ))}
          </div>
        )}

        {loading ? (
          <div className="flex items-center justify-center py-24">
            <div className="h-8 w-8 animate-spin rounded-full border-4"
              style={{ borderColor: "rgba(0,0,0,0.08)", borderTopColor: S.accent }} />
          </div>
        ) : samples.length === 0 ? (
          <p className="py-24 text-center text-sm" style={{ color: S.text3 }}>{t("暂无金样本")}</p>
        ) : groupByRule ? (
          /* Grouped view */
          Object.entries(grouped).sort().map(([rule, items]) => (
            <section key={rule}>
              <h2 className="mb-3 flex items-center gap-2">
                <span className="rounded-lg px-2.5 py-1 text-xs font-semibold"
                  style={{ background: S.accentBg, color: S.accent, border: "1px solid rgba(184,146,46,0.25)" }}>
                  {rule}
                </span>
                <span className="text-xs" style={{ color: S.text3 }}>{items.length} {t("个")}</span>
              </h2>
              <div className="space-y-2">
                {items.map((s) => <SampleCard key={s.id} sample={s} onDelete={handleDelete} t={t} />)}
              </div>
            </section>
          ))
        ) : (
          /* Flat view */
          <div className="space-y-2">
            {samples.map((s) => <SampleCard key={s.id} sample={s} onDelete={handleDelete} t={t} />)}
          </div>
        )}
      </div>

      {toast && <Toast msg={toast} onClose={() => setToast("")} />}
    </div>
  );
}

function SampleCard({ sample: s, onDelete, t }: { sample: GoldenSample; onDelete: (id: number) => void; t: (k: string) => string }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="rounded-xl overflow-hidden transition-all"
      style={{ background: S.overlay, border: `1px solid ${S.border}` }}>
      <div className="flex items-start gap-3 px-4 py-3 cursor-pointer"
        onClick={() => setExpanded(!expanded)}
        onMouseEnter={(e) => (e.currentTarget.style.background = S.surface)}
        onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
      >
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-sm font-medium" style={{ color: S.text1 }}>{s.problem_type || "—"}</span>
            <ConfBadge c={s.confidence} />
            {s.rule_type && (
              <span className="rounded-full px-2 py-0.5 text-[10px] font-medium"
                style={{ background: S.accentBg, color: S.accent, border: "1px solid rgba(184,146,46,0.25)" }}>
                {s.rule_type}
              </span>
            )}
          </div>
          <p className="text-xs leading-relaxed" style={{
            color: S.text2,
            display: "-webkit-box", WebkitLineClamp: expanded ? 99 : 2,
            WebkitBoxOrient: "vertical", overflow: "hidden",
          }}>{s.description}</p>
        </div>
        <div className="flex-shrink-0 text-right">
          <p className="font-mono text-[10px]" style={{ color: S.text3 }}>{formatLocalTime(s.created_at)}</p>
          {s.created_by && <p className="text-[10px] mt-0.5" style={{ color: S.text3 }}>{s.created_by}</p>}
        </div>
      </div>

      {expanded && (
        <div className="px-4 pb-4 space-y-3" style={{ borderTop: `1px solid ${S.border}` }}>
          <div className="pt-3">
            <p className="text-[10px] font-semibold uppercase tracking-wider mb-1" style={{ color: S.text3 }}>{t("根因")}</p>
            <p className="text-sm whitespace-pre-wrap" style={{ color: S.text2 }}>{s.root_cause}</p>
          </div>
          {s.user_reply && (
            <div>
              <p className="text-[10px] font-semibold uppercase tracking-wider mb-1" style={{ color: S.text3 }}>{t("建议回复")}</p>
              <div className="rounded-lg p-3 text-sm whitespace-pre-wrap"
                style={{ background: S.surface, color: S.text2, borderLeft: "2px solid rgba(34,197,94,0.4)" }}>
                {s.user_reply}
              </div>
            </div>
          )}
          <div className="flex items-center justify-between pt-2">
            <span className="text-[10px] font-mono" style={{ color: S.text3 }}>
              ID: {s.id} | Issue: {s.issue_id}
            </span>
            <button onClick={(e) => { e.stopPropagation(); onDelete(s.id); }}
              className="rounded-lg px-3 py-1 text-[11px] font-medium"
              style={{ background: "rgba(239,68,68,0.10)", color: "#DC2626", border: "1px solid rgba(239,68,68,0.25)" }}>
              {t("删除")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
