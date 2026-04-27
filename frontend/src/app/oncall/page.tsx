"use client";

import { useT } from "@/lib/i18n";
import { useEffect, useState } from "react";
import { Toast } from "@/components/Toast";
import {
  getOncallSchedule, getOncallCurrent, updateOncallSchedule,
  getOncallTickets, resolveOncallTicket,
  type EscalatedTicket,
} from "@/lib/api";

const S = {
  surface: "#F8F9FA", overlay: "#FFFFFF", hover: "#EEF0F2",
  border: "rgba(0,0,0,0.08)", accent: "#B8922E", accentBg: "rgba(184,146,46,0.06)",
  text1: "#111827", text2: "#6B7280", text3: "#9CA3AF",
};

const inputStyle = { background: S.overlay, border: `1px solid ${S.border}`, color: S.text1, outline: "none" };

function formatTime(iso: string) {
  if (!iso) return "";
  const d = new Date(iso);
  return `${(d.getMonth() + 1).toString().padStart(2, "0")}-${d.getDate().toString().padStart(2, "0")} ${d.getHours().toString().padStart(2, "0")}:${d.getMinutes().toString().padStart(2, "0")}`;
}

function truncate(s: string, max: number) {
  return s.length > max ? s.slice(0, max) + "..." : s;
}

function weekGroupIndex(dateStr: string, startDate: string, totalGroups: number): number {
  if (!startDate || totalGroups === 0) return -1;
  const start = new Date(startDate).getTime();
  const target = new Date(dateStr).getTime();
  const weeks = Math.floor((target - start) / (7 * 86400000));
  return ((weeks % totalGroups) + totalGroups) % totalGroups;
}

export default function OncallPage() {
  const t = useT();
  const [groups, setGroups] = useState<string[][]>([]);
  const [startDate, setStartDate] = useState("");
  const [currentMembers, setCurrentMembers] = useState<string[]>([]);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState<{ msg: string; type: "success" | "error" } | null>(null);

  const [selectedGroup, setSelectedGroup] = useState<number>(-1);
  const [allTickets, setAllTickets] = useState<EscalatedTicket[]>([]);
  const [ticketsLoading, setTicketsLoading] = useState(false);
  const [resolving, setResolving] = useState<string | null>(null);

  const username = typeof window !== "undefined" ? localStorage.getItem("appllo_username") || "" : "";
  const isAdmin = username === "sanato";

  const currentGroupIdx = (() => {
    if (!startDate || groups.length === 0) return -1;
    const start = new Date(startDate);
    const today = new Date();
    const weeks = Math.floor((today.getTime() - start.getTime()) / (7 * 86400000));
    return weeks % groups.length;
  })();

  const load = async () => {
    try {
      const [sched, curr] = await Promise.all([getOncallSchedule(), getOncallCurrent()]);
      setGroups(sched.groups.map((g) => g.members));
      setStartDate(sched.start_date || new Date().toISOString().slice(0, 10));
      setCurrentMembers(curr.members);
    } catch (e: any) { setToast({ msg: e.message, type: "error" }); }
  };

  const loadTickets = async () => {
    setTicketsLoading(true);
    try {
      const res = await getOncallTickets(undefined, Math.max(groups.length, 4));
      setAllTickets(res.tickets);
    } catch (e: any) { setToast({ msg: e.message, type: "error" }); }
    finally { setTicketsLoading(false); }
  };

  useEffect(() => { load(); }, []);
  useEffect(() => {
    if (currentGroupIdx >= 0 && selectedGroup === -1) setSelectedGroup(currentGroupIdx);
  }, [currentGroupIdx, selectedGroup]);
  useEffect(() => {
    if (groups.length > 0) loadTickets();
  }, [groups.length]);

  const groupTickets = allTickets.filter((tk) => {
    if (selectedGroup < 0 || !startDate || groups.length === 0) return false;
    return weekGroupIndex(tk.escalated_at, startDate, groups.length) === selectedGroup;
  });
  const inProgressTickets = groupTickets.filter((tk) => tk.escalation_status !== "resolved");
  const resolvedTickets = groupTickets.filter((tk) => tk.escalation_status === "resolved");

  const handleResolve = async (issueId: string) => {
    setResolving(issueId);
    try {
      const res = await resolveOncallTicket(issueId);
      setAllTickets((prev) => prev.map((tk) =>
        tk.record_id === issueId
          ? { ...tk, escalation_status: "resolved", escalation_resolved_at: new Date().toISOString() }
          : tk
      ));
      setToast({
        msg: res.feishu_notified ? t("工单已标记完成，已通知飞书群") : t("工单已标记完成"),
        type: "success",
      });
    } catch (e: any) { setToast({ msg: e.message, type: "error" }); }
    finally { setResolving(null); }
  };

  // Editing helpers
  const addGroup = () => setGroups((p) => [...p, [""]]);
  const removeGroup = (idx: number) => setGroups((p) => p.filter((_, i) => i !== idx));
  const updateMember = (gi: number, mi: number, val: string) => {
    setGroups((p) => p.map((g, i) => i === gi ? g.map((m, j) => j === mi ? val : m) : g));
  };
  const addMember = (gi: number) => setGroups((p) => p.map((g, i) => i === gi ? [...g, ""] : g));
  const removeMember = (gi: number, mi: number) => {
    setGroups((p) => p.map((g, i) => i === gi ? g.filter((_, j) => j !== mi) : g));
  };

  const save = async () => {
    const cleaned = groups.map((g) => g.filter((m) => m.trim())).filter((g) => g.length > 0);
    if (!cleaned.length) { setToast({ msg: t("至少需要一组值班人员"), type: "error" }); return; }
    if (!startDate) { setToast({ msg: t("请设置起始日期"), type: "error" }); return; }
    setSaving(true);
    try {
      await updateOncallSchedule(cleaned, startDate, username);
      setToast({ msg: t("值班表已保存"), type: "success" });
      setEditing(false); await load();
    } catch (e: any) { setToast({ msg: e.message, type: "error" }); }
    finally { setSaving(false); }
  };

  // Count tickets per group
  const groupTicketCount = (gi: number) =>
    allTickets.filter((tk) =>
      weekGroupIndex(tk.escalated_at, startDate, groups.length) === gi
      && tk.escalation_status !== "resolved"
    ).length;

  // =========================================================================
  // RENDER
  // =========================================================================

  // Edit mode — full-width vertical layout (same as before)
  if (editing) {
    return (
      <div className="min-h-full">
        <header className="sticky top-0 z-10 backdrop-blur-md"
          style={{ background: "rgba(255,255,255,0.92)", borderBottom: `1px solid ${S.border}` }}>
          <div className="flex items-center justify-between px-6 py-3">
            <div>
              <h1 className="text-base font-semibold" style={{ color: S.text1 }}>{t("值班管理")}</h1>
              <p className="text-xs mt-0.5" style={{ color: S.text3 }}>{t("编辑排班中")}</p>
            </div>
            <div className="flex items-center gap-2">
              <button onClick={save} disabled={saving}
                className="rounded-lg px-4 py-1.5 text-sm font-semibold disabled:opacity-50"
                style={{ background: S.accent, color: "#0A0B0E" }}>
                {saving ? t("保存中...") : t("保存")}
              </button>
              <button onClick={() => { setEditing(false); load(); }}
                className="rounded-lg px-3 py-1.5 text-sm font-medium"
                style={{ border: `1px solid ${S.border}`, color: S.text2 }}>
                {t("取消")}
              </button>
            </div>
          </div>
        </header>
        <div className="mx-auto max-w-3xl px-6 py-6 space-y-5">
          <section className="rounded-xl p-5" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
            <h2 className="mb-3 text-sm font-semibold" style={{ color: S.text1 }}>{t("轮换起始日期")}</h2>
            <input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)}
              className="rounded-lg px-3 py-2 text-sm font-sans outline-none" style={inputStyle} />
            <p className="mt-1.5 text-xs" style={{ color: S.text3 }}>{t("从此日期开始，每周一轮换到下一组")}</p>
          </section>
          <section className="rounded-xl p-5" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-sm font-semibold" style={{ color: S.text1 }}>{t("值班分组")}</h2>
              <button onClick={addGroup} className="rounded-lg px-3 py-1 text-xs font-medium"
                style={{ background: S.accentBg, color: S.accent, border: "1px solid rgba(184,146,46,0.25)" }}>
                {t("添加分组")}
              </button>
            </div>
            <div className="space-y-3">
              {groups.map((members, gi) => (
                <div key={gi} className="rounded-xl p-4" style={{ background: S.overlay, border: `1px solid ${S.border}` }}>
                  <div className="flex items-center justify-between mb-2">
                    <span className="text-xs font-semibold" style={{ color: S.text1 }}>{t("第")} {gi + 1} {t("组")}</span>
                    {groups.length > 1 && (
                      <button onClick={() => removeGroup(gi)} className="text-xs" style={{ color: "#DC2626" }}>{t("删除分组")}</button>
                    )}
                  </div>
                  <div className="space-y-2">
                    {members.map((email, mi) => (
                      <div key={mi} className="flex items-center gap-2">
                        <input value={email} onChange={(e) => updateMember(gi, mi, e.target.value)}
                          placeholder={t("飞书邮箱，如 engineer@plaud.ai")}
                          className="flex-1 rounded-lg px-3 py-1.5 text-sm font-sans outline-none" style={inputStyle} />
                        <button onClick={() => removeMember(gi, mi)} className="text-xs" style={{ color: S.text3 }}>{t("移除")}</button>
                      </div>
                    ))}
                    <button onClick={() => addMember(gi)} className="text-xs" style={{ color: "#2563EB" }}>+ {t("添加成员")}</button>
                  </div>
                </div>
              ))}
            </div>
          </section>
        </div>
        {toast && <Toast msg={toast.msg} type={toast.type} onClose={() => setToast(null)} />}
      </div>
    );
  }

  // View mode — left sidebar groups + right ticket content
  return (
    <div className="min-h-full">
      <header className="sticky top-0 z-10 backdrop-blur-md"
        style={{ background: "rgba(255,255,255,0.92)", borderBottom: `1px solid ${S.border}` }}>
        <div className="flex items-center justify-between px-6 py-3">
          <div>
            <h1 className="text-base font-semibold" style={{ color: S.text1 }}>{t("值班管理")}</h1>
            <p className="text-xs mt-0.5" style={{ color: S.text3 }}>{t("每周轮换，自动通知值班工程师")}</p>
          </div>
          {isAdmin && (
            <button onClick={() => setEditing(true)}
              className="rounded-lg px-3 py-1.5 text-sm font-medium transition-colors"
              style={{ border: `1px solid ${S.border}`, color: S.text2 }}>
              {t("编辑排班")}
            </button>
          )}
        </div>
      </header>

      <div className="flex" style={{ minHeight: "calc(100vh - 52px)" }}>
        {/* Left sidebar — group list (vertical) */}
        <aside className="w-52 flex-shrink-0 overflow-y-auto py-4 px-3 space-y-1.5"
          style={{ borderRight: `1px solid ${S.border}`, background: S.surface }}>
          <p className="px-2 mb-2 text-[10px] font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>
            {t("值班分组")}
          </p>
          {groups.map((members, gi) => {
            const isCurrent = gi === currentGroupIdx;
            const isSelected = gi === selectedGroup;
            const count = groupTicketCount(gi);

            return (
              <button key={gi}
                onClick={() => setSelectedGroup(gi)}
                className="w-full rounded-lg px-3 py-2.5 text-left transition-all"
                style={{
                  background: isSelected
                    ? isCurrent ? "rgba(34,197,94,0.12)" : S.overlay
                    : "transparent",
                  border: isSelected
                    ? `1.5px solid ${isCurrent ? "rgba(34,197,94,0.4)" : S.border}`
                    : "1.5px solid transparent",
                }}>
                <div className="flex items-center justify-between mb-1">
                  <span className="text-xs font-semibold" style={{ color: isSelected ? S.text1 : S.text2 }}>
                    {t("第")} {gi + 1} {t("组")}
                  </span>
                  <div className="flex items-center gap-1">
                    {isCurrent && (
                      <span className="rounded-full px-1.5 py-0.5 text-[8px] font-bold"
                        style={{ background: "rgba(34,197,94,0.15)", color: "#16A34A" }}>
                        {t("本周")}
                      </span>
                    )}
                    {count > 0 && (
                      <span className="rounded-full min-w-[16px] h-4 flex items-center justify-center text-[9px] font-bold"
                        style={{ background: "rgba(234,179,8,0.18)", color: "#B45309" }}>
                        {count}
                      </span>
                    )}
                  </div>
                </div>
                <div className="space-y-0.5">
                  {members.map((m) => (
                    <p key={m} className="text-[11px] truncate"
                      style={{ color: isCurrent && isSelected ? "#16A34A" : S.text3 }}>
                      {m.split("@")[0]}
                    </p>
                  ))}
                </div>
              </button>
            );
          })}

          {/* Start date footnote */}
          {startDate && (
            <p className="px-2 pt-3 text-[9px]" style={{ color: S.text3, borderTop: `1px solid ${S.border}` }}>
              {t("起始")}: {startDate}
            </p>
          )}
        </aside>

        {/* Right content — tickets */}
        <main className="flex-1 overflow-y-auto p-6">
          {selectedGroup < 0 ? (
            <div className="flex items-center justify-center h-full">
              <p className="text-sm" style={{ color: S.text3 }}>{t("点击左侧分组查看工单")}</p>
            </div>
          ) : (
            <>
              {/* Header */}
              <div className="flex items-center justify-between mb-4">
                <div className="flex items-center gap-3">
                  <h2 className="text-sm font-semibold" style={{ color: S.text1 }}>
                    {t("第")} {selectedGroup + 1} {t("组")} — {t("转交工单")}
                  </h2>
                  {selectedGroup === currentGroupIdx && (
                    <span className="rounded-full px-2 py-0.5 text-[9px] font-bold"
                      style={{ background: "rgba(34,197,94,0.15)", color: "#16A34A" }}>
                      {t("本周")}
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-3 text-xs">
                  <span style={{ color: "#B45309" }}>{inProgressTickets.length} {t("进行中")}</span>
                  <span style={{ color: "#16A34A" }}>{resolvedTickets.length} {t("已完成")}</span>
                </div>
              </div>

              {/* Members bar */}
              <div className="flex flex-wrap gap-1.5 mb-4">
                {(groups[selectedGroup] || []).map((m) => (
                  <span key={m} className="rounded-lg px-2.5 py-1 text-[11px] font-medium"
                    style={{
                      background: selectedGroup === currentGroupIdx ? "rgba(34,197,94,0.08)" : S.surface,
                      color: selectedGroup === currentGroupIdx ? "#16A34A" : S.text2,
                      border: `1px solid ${selectedGroup === currentGroupIdx ? "rgba(34,197,94,0.2)" : S.border}`,
                    }}>
                    {m}
                  </span>
                ))}
              </div>

              {/* Tickets */}
              {ticketsLoading ? (
                <p className="py-12 text-center text-xs" style={{ color: S.text3 }}>{t("加载中...")}</p>
              ) : groupTickets.length === 0 ? (
                <div className="rounded-xl py-16 text-center" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
                  <p className="text-sm" style={{ color: S.text3 }}>{t("暂无转交工单")}</p>
                </div>
              ) : (
                <div className="space-y-3">
                  {[...inProgressTickets, ...resolvedTickets].map((tk) => (
                    <div key={tk.record_id} className="rounded-xl p-4"
                      style={{ background: S.overlay, border: `1px solid ${S.border}` }}>
                      <div className="flex items-center justify-between mb-2">
                        <div className="flex items-center gap-2">
                          {tk.problem_type && (
                            <span className="rounded-full px-2 py-0.5 text-[10px] font-medium"
                              style={{ background: "rgba(99,102,241,0.1)", color: "#6366F1", border: "1px solid rgba(99,102,241,0.2)" }}>
                              {tk.problem_type}
                            </span>
                          )}
                          {tk.zendesk_id && (
                            <span className="text-[10px] font-mono" style={{ color: S.text3 }}>#{tk.zendesk_id}</span>
                          )}
                        </div>
                        <span className="rounded-full px-2 py-0.5 text-[10px] font-medium"
                          style={tk.escalation_status === "resolved"
                            ? { background: "rgba(34,197,94,0.12)", color: "#16A34A", border: "1px solid rgba(34,197,94,0.25)" }
                            : { background: "rgba(234,179,8,0.12)", color: "#B45309", border: "1px solid rgba(234,179,8,0.25)" }}>
                          {tk.escalation_status === "resolved" ? t("已完成") : t("进行中")}
                        </span>
                      </div>

                      <p className="text-xs leading-relaxed mb-2" style={{ color: S.text1 }}>
                        {truncate(tk.description, 150)}
                      </p>

                      {tk.root_cause && (
                        <div className="rounded-lg p-2.5 mb-2" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
                          <p className="text-[10px] font-medium mb-0.5" style={{ color: S.text3 }}>{t("根因")}</p>
                          <p className="text-xs" style={{ color: S.text2 }}>{truncate(tk.root_cause, 150)}</p>
                        </div>
                      )}

                      {tk.escalation_note && (
                        <div className="rounded-lg p-2.5 mb-2" style={{ background: "rgba(234,179,8,0.04)", border: "1px solid rgba(234,179,8,0.15)" }}>
                          <p className="text-[10px] font-medium mb-0.5" style={{ color: "#B45309" }}>{t("转交备注")}</p>
                          <p className="text-xs" style={{ color: S.text2 }}>{tk.escalation_note}</p>
                        </div>
                      )}

                      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] mb-2" style={{ color: S.text3 }}>
                        <span>{t("转交人")}: {tk.escalated_by}</span>
                        <span>{t("转交时间")}: {formatTime(tk.escalated_at)}</span>
                        {tk.escalation_status === "resolved" && tk.escalation_resolved_at && (
                          <span style={{ color: "#16A34A" }}>{t("完成于")} {formatTime(tk.escalation_resolved_at)}</span>
                        )}
                      </div>

                      <div className="flex items-center gap-2 pt-2" style={{ borderTop: `1px solid ${S.border}` }}>
                        <a href={`/tracking?detail=${tk.record_id}`}
                          className="rounded-lg px-3 py-1.5 text-[11px] font-medium"
                          style={{ color: "#2563EB", background: "rgba(37,99,235,0.06)", border: "1px solid rgba(37,99,235,0.15)" }}>
                          {t("查看详情")}
                        </a>
                        {tk.escalation_status !== "resolved" && (
                          <button onClick={() => handleResolve(tk.record_id)}
                            disabled={resolving === tk.record_id}
                            className="rounded-lg px-3 py-1.5 text-[11px] font-medium disabled:opacity-50"
                            style={{ background: "rgba(34,197,94,0.1)", color: "#16A34A", border: "1px solid rgba(34,197,94,0.25)" }}>
                            {resolving === tk.record_id ? t("处理中...") : t("标记完成")}
                          </button>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </main>
      </div>

      {toast && <Toast msg={toast.msg} type={toast.type} onClose={() => setToast(null)} />}
    </div>
  );
}
