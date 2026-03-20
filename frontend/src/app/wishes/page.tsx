"use client";

import { useEffect, useState } from "react";
import { useT } from "@/lib/i18n";
import {
  fetchWishes,
  createWish,
  voteWish,
  deleteWish,
  updateWish,
  formatLocalTime,
  type Wish,
} from "@/lib/api";

const S = {
  surface: "#F8F9FA", overlay: "#FFFFFF", hover: "#EEF0F2",
  border: "rgba(0,0,0,0.08)", borderSm: "rgba(0,0,0,0.04)",
  accent: "#B8922E", accentBg: "rgba(184,146,46,0.06)",
  text1: "#111827", text2: "#6B7280", text3: "#9CA3AF",
  danger: "#DC2626", dangerBg: "rgba(220,38,38,0.06)",
};

const STATUS_STYLES: Record<string, { bg: string; color: string; border: string }> = {
  pending: { bg: "rgba(251,146,60,0.12)", color: "#EA580C", border: "rgba(251,146,60,0.25)" },
  accepted: { bg: "rgba(59,130,246,0.12)", color: "#2563EB", border: "rgba(59,130,246,0.25)" },
  done: { bg: "rgba(34,197,94,0.12)", color: "#16A34A", border: "rgba(34,197,94,0.25)" },
  rejected: { bg: "rgba(107,114,128,0.12)", color: "#6B7280", border: "rgba(107,114,128,0.25)" },
};

function StatusBadge({ status, t }: { status: string; t: (k: string) => string }) {
  const s = STATUS_STYLES[status] || STATUS_STYLES.pending;
  const labels: Record<string, string> = {
    pending: "待评估", accepted: "已采纳", done: "已实现", rejected: "已拒绝",
  };
  return (
    <span className="inline-flex rounded-full px-2 py-0.5 text-[10px] font-medium"
      style={{ background: s.bg, color: s.color, border: `1px solid ${s.border}` }}>
      {t(labels[status] || status)}
    </span>
  );
}

function Toast({ msg, onClose }: { msg: string; onClose: () => void }) {
  useEffect(() => { const id = setTimeout(onClose, 2500); return () => clearTimeout(id); }, [onClose]);
  return (
    <div className="fixed bottom-6 right-6 z-50 rounded-xl px-4 py-2.5 text-sm font-medium shadow-2xl"
      style={{ background: S.overlay, color: S.text1, border: `1px solid ${S.border}` }}>
      {msg}
    </div>
  );
}

export default function WishesPage() {
  const t = useT();
  const [wishes, setWishes] = useState<Wish[]>([]);
  const [loading, setLoading] = useState(true);
  const [toast, setToast] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [filterStatus, setFilterStatus] = useState("");
  const [title, setTitle] = useState("");
  const [desc, setDesc] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const data = await fetchWishes();
      setWishes(data);
    } catch {} finally { setLoading(false); }
  };

  useEffect(() => { load(); }, []);

  const handleSubmit = async () => {
    if (!title.trim()) return;
    setSubmitting(true);
    try {
      const username = typeof window !== "undefined" ? localStorage.getItem("jarvis_username") || "" : "";
      await createWish({ title: title.trim(), description: desc.trim(), created_by: username });
      setToast(t("许愿已提交"));
      setTitle(""); setDesc(""); setShowForm(false);
      load();
    } catch {} finally { setSubmitting(false); }
  };

  const handleVote = async (id: number) => {
    try {
      const updated = await voteWish(id);
      setWishes(prev => prev.map(w => w.id === id ? updated : w));
    } catch {}
  };

  const handleDelete = async (id: number) => {
    if (!confirm(t("确定要删除这个许愿吗？"))) return;
    try {
      await deleteWish(id);
      setToast(t("许愿已删除"));
      load();
    } catch {}
  };

  const handleStatusChange = async (id: number, status: string) => {
    try {
      const updated = await updateWish(id, { status });
      setWishes(prev => prev.map(w => w.id === id ? updated : w));
    } catch {}
  };

  const filtered = filterStatus ? wishes.filter(w => w.status === filterStatus) : wishes;

  const stats = {
    total: wishes.length,
    pending: wishes.filter(w => w.status === "pending").length,
    accepted: wishes.filter(w => w.status === "accepted").length,
    done: wishes.filter(w => w.status === "done").length,
  };

  return (
    <div className="min-h-full">
      {/* Header */}
      <header className="sticky top-0 z-10 backdrop-blur-md"
        style={{ background: "rgba(255,255,255,0.92)", borderBottom: `1px solid ${S.border}` }}>
        <div className="flex items-center justify-between px-6 py-3">
          <div>
            <h1 className="text-base font-semibold" style={{ color: S.text1 }}>{t("许愿池")}</h1>
            <p className="text-xs mt-0.5" style={{ color: S.text3 }}>{t("提交你想要的功能或改进")}</p>
          </div>
          <div className="flex items-center gap-2">
            <select value={filterStatus} onChange={e => setFilterStatus(e.target.value)}
              className="rounded-lg px-3 py-1.5 text-sm outline-none"
              style={{ background: S.overlay, border: `1px solid ${S.border}`, color: S.text1 }}>
              <option value="">{t("全部状态")}</option>
              <option value="pending">{t("待评估")}</option>
              <option value="accepted">{t("已采纳")}</option>
              <option value="done">{t("已实现")}</option>
              <option value="rejected">{t("已拒绝")}</option>
            </select>
            <button onClick={() => setShowForm(!showForm)}
              className="rounded-lg px-4 py-1.5 text-sm font-medium transition-all"
              style={{ background: S.accent, color: "#FFFFFF" }}>
              {t("新增许愿")}
            </button>
          </div>
        </div>
      </header>

      <div className="mx-auto max-w-3xl px-6 py-6 space-y-5">
        {/* Stats */}
        <div className="grid grid-cols-4 gap-3">
          {[
            { label: t("全部"), value: stats.total, color: S.text1 },
            { label: t("待评估"), value: stats.pending, color: "#EA580C" },
            { label: t("已采纳"), value: stats.accepted, color: "#2563EB" },
            { label: t("已实现"), value: stats.done, color: "#16A34A" },
          ].map(s => (
            <div key={s.label} className="rounded-xl px-4 py-3" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
              <p className="text-xs" style={{ color: S.text3 }}>{s.label}</p>
              <p className="mt-1 text-2xl font-bold tabular-nums" style={{ color: s.color }}>{s.value}</p>
            </div>
          ))}
        </div>

        {/* New wish form */}
        {showForm && (
          <div className="rounded-xl p-5 space-y-3" style={{ background: S.overlay, border: `1px solid ${S.accent}`, boxShadow: "0 4px 24px rgba(184,146,46,0.08)" }}>
            <input
              value={title}
              onChange={e => setTitle(e.target.value)}
              placeholder={t("请输入你的愿望...")}
              className="w-full rounded-lg px-4 py-2.5 text-sm outline-none"
              style={{ background: S.surface, border: `1px solid ${S.border}`, color: S.text1 }}
              onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSubmit(); } }}
              autoFocus
            />
            <textarea
              value={desc}
              onChange={e => setDesc(e.target.value)}
              placeholder={t("描述你想要的功能或改进...")}
              rows={3}
              className="w-full rounded-lg px-4 py-2.5 text-sm outline-none resize-none"
              style={{ background: S.surface, border: `1px solid ${S.border}`, color: S.text1 }}
            />
            <div className="flex justify-end gap-2">
              <button onClick={() => { setShowForm(false); setTitle(""); setDesc(""); }}
                className="rounded-lg px-4 py-1.5 text-sm font-medium"
                style={{ color: S.text2, background: S.surface }}>
                {t("取消")}
              </button>
              <button onClick={handleSubmit} disabled={submitting || !title.trim()}
                className="rounded-lg px-4 py-1.5 text-sm font-medium transition-all disabled:opacity-40"
                style={{ background: S.accent, color: "#FFFFFF" }}>
                {submitting ? t("提交中...") : t("提交许愿")}
              </button>
            </div>
          </div>
        )}

        {/* Wish list */}
        {loading ? (
          <div className="flex items-center justify-center py-24">
            <div className="h-8 w-8 animate-spin rounded-full border-4"
              style={{ borderColor: "rgba(0,0,0,0.08)", borderTopColor: S.accent }} />
          </div>
        ) : filtered.length === 0 ? (
          <div className="py-24 text-center">
            <p className="text-4xl mb-3">&#10024;</p>
            <p className="text-sm font-medium" style={{ color: S.text2 }}>{t("暂无许愿")}</p>
            <p className="text-xs mt-1" style={{ color: S.text3 }}>{t("成为第一个许愿的人吧")}</p>
          </div>
        ) : (
          <div className="space-y-2">
            {filtered.map(wish => (
              <WishCard key={wish.id} wish={wish} t={t}
                onVote={handleVote} onDelete={handleDelete} onStatusChange={handleStatusChange} />
            ))}
          </div>
        )}
      </div>

      {toast && <Toast msg={toast} onClose={() => setToast("")} />}
    </div>
  );
}

function WishCard({ wish, t, onVote, onDelete, onStatusChange }: {
  wish: Wish;
  t: (k: string) => string;
  onVote: (id: number) => void;
  onDelete: (id: number) => void;
  onStatusChange: (id: number, status: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="rounded-xl overflow-hidden transition-all"
      style={{ background: S.overlay, border: `1px solid ${S.border}` }}>
      <div className="flex items-start gap-3 px-4 py-3">
        {/* Vote button */}
        <button onClick={() => onVote(wish.id)}
          className="flex flex-col items-center gap-0.5 pt-0.5 rounded-lg px-2 py-1.5 transition-all flex-shrink-0"
          style={{ background: S.surface, border: `1px solid ${S.borderSm}` }}
          onMouseEnter={e => { e.currentTarget.style.borderColor = S.accent; }}
          onMouseLeave={e => { e.currentTarget.style.borderColor = S.borderSm; }}>
          <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke={S.accent} strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M5 15l7-7 7 7" />
          </svg>
          <span className="text-xs font-bold tabular-nums" style={{ color: S.accent }}>{wish.votes}</span>
        </button>

        {/* Content */}
        <div className="flex-1 min-w-0 cursor-pointer" onClick={() => setExpanded(!expanded)}>
          <div className="flex items-center gap-2 mb-1">
            <span className="text-sm font-medium" style={{ color: S.text1 }}>{wish.title}</span>
            <StatusBadge status={wish.status} t={t} />
          </div>
          {wish.description && (
            <p className="text-xs leading-relaxed" style={{
              color: S.text2,
              display: "-webkit-box", WebkitLineClamp: expanded ? 99 : 2,
              WebkitBoxOrient: "vertical", overflow: "hidden",
            }}>{wish.description}</p>
          )}
          <div className="flex items-center gap-3 mt-1.5">
            <span className="font-mono text-[10px]" style={{ color: S.text3 }}>{formatLocalTime(wish.created_at)}</span>
            {wish.created_by && <span className="text-[10px]" style={{ color: S.text3 }}>{wish.created_by}</span>}
          </div>
        </div>
      </div>

      {/* Expanded actions */}
      {expanded && (
        <div className="flex items-center justify-between px-4 py-2.5" style={{ borderTop: `1px solid ${S.border}`, background: S.surface }}>
          <div className="flex items-center gap-1.5">
            {["pending", "accepted", "done", "rejected"].map(st => (
              <button key={st}
                onClick={() => onStatusChange(wish.id, st)}
                className="rounded-md px-2.5 py-1 text-[11px] font-medium transition-all"
                style={wish.status === st
                  ? { background: (STATUS_STYLES[st] || STATUS_STYLES.pending).bg, color: (STATUS_STYLES[st] || STATUS_STYLES.pending).color, border: `1px solid ${(STATUS_STYLES[st] || STATUS_STYLES.pending).border}` }
                  : { background: "transparent", color: S.text3, border: `1px solid ${S.borderSm}` }
                }>
                {t({ pending: "待评估", accepted: "已采纳", done: "已实现", rejected: "已拒绝" }[st] || st)}
              </button>
            ))}
          </div>
          <button onClick={() => onDelete(wish.id)}
            className="rounded-lg px-3 py-1 text-[11px] font-medium"
            style={{ background: S.dangerBg, color: S.danger, border: "1px solid rgba(220,38,38,0.25)" }}>
            {t("删除")}
          </button>
        </div>
      )}
    </div>
  );
}
