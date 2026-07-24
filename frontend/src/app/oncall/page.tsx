"use client";

import { useT } from "@/lib/i18n";
import { useEffect, useRef, useState } from "react";
import { Toast } from "@/components/Toast";
import {
  getOncallSchedule, getOncallCurrent, updateOncallSchedule,
  getOncallTickets, resolveOncallTicket, getOncallStats, getOncallWeekGroups, getOncallFeishuTickets, resolveFeishuTicket,
  createTask,
  type EscalatedTicket, type OncallWeekStat, type OncallWeekGroupEntry, type Issue,
} from "@/lib/api";

const S = {
  surface: "var(--j-surface)", overlay: "var(--j-panel)", hover: "var(--j-hover)",
  border: "var(--j-border)", accent: "var(--j-accent)", accentBg: "var(--j-accent-soft)",
  text1: "var(--j-ink)", text2: "var(--j-graphite)", text3: "var(--j-faint)",
};

const inputStyle = { background: S.overlay, border: `1px solid ${S.border}`, color: S.text1, outline: "none" };

// Ticket source → label key (i18n) + color. Covers feishu / linear / local / api.
const SOURCE_META: Record<string, { key: string; bg: string; fg: string; bd: string }> = {
  feishu: { key: "飞书", bg: "rgba(59,130,246,0.1)", fg: "#2563EB", bd: "rgba(59,130,246,0.22)" },
  linear: { key: "Linear", bg: "rgba(139,92,246,0.1)", fg: "#7C3AED", bd: "rgba(139,92,246,0.22)" },
  local: { key: "本地表单", bg: "rgba(16,185,129,0.1)", fg: "#059669", bd: "rgba(16,185,129,0.22)" },
  api: { key: "API", bg: "rgba(107,114,128,0.12)", fg: "#4B5563", bd: "rgba(107,114,128,0.22)" },
};
function SourceBadge({ source, t }: { source?: string; t: (k: string) => string }) {
  const m = SOURCE_META[source || "feishu"] || SOURCE_META.feishu;
  return (
    <span className="rounded-full px-2 py-0.5 text-[10px] font-semibold"
      style={{ background: m.bg, color: m.fg, border: `1px solid ${m.bd}` }}>
      {t(m.key)}
    </span>
  );
}

function formatTime(iso: string) {
  if (!iso) return "";
  const d = new Date(iso);
  return `${(d.getMonth() + 1).toString().padStart(2, "0")}-${d.getDate().toString().padStart(2, "0")} ${d.getHours().toString().padStart(2, "0")}:${d.getMinutes().toString().padStart(2, "0")}`;
}
function formatDate(iso: string) {
  if (!iso) return "";
  return iso.slice(5); // MM-DD
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
function weekNum(dateStr: string, startDate: string): number {
  if (!startDate) return -1;
  const start = new Date(startDate).getTime();
  const target = new Date(dateStr).getTime();
  return Math.floor((target - start) / (7 * 86400000));
}

export default function OncallPage() {
  const t = useT();
  const [groups, setGroups] = useState<string[][]>([]);
  const [startDate, setStartDate] = useState("");
  const [currentMembers, setCurrentMembers] = useState<string[]>([]);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState<{ msg: string; type: "success" | "error" } | null>(null);

  // Sidebar: select a group or a specific week
  const [selectedGroup, setSelectedGroup] = useState<number>(-1);
  const [selectedWeek, setSelectedWeek] = useState<number | null>(null); // null = show all for group
  const [allTickets, setAllTickets] = useState<EscalatedTicket[]>([]);
  const [ticketsLoading, setTicketsLoading] = useState(false);
  const [resolving, setResolving] = useState<string | null>(null);

  // Stats
  const [weekStats, setWeekStats] = useState<OncallWeekStat[]>([]);
  const [tab, setTab] = useState<"tickets" | "stats">("tickets");

  // 2026-07-24：周→组的权威映射(后端排班快照优先，查不到才现算)，替代前端本地
  // 重新实现一遍取模逻辑——避免"新增/删除值班组"时前端和后端各算一套导致不一致。
  const [weekGroups, setWeekGroups] = useState<OncallWeekGroupEntry[]>([]);
  const [currentGroupIndexFromApi, setCurrentGroupIndexFromApi] = useState<number | null>(null);

  // Show resolved/done tickets (off by default → only pending + in_progress shown)
  const [showResolved, setShowResolved] = useState(false);

  // Feishu tickets (handled directly in Feishu) for ALL assignees — filtered per group client-side
  const [feishuTickets, setFeishuTickets] = useState<Issue[]>([]);
  const [feishuDone, setFeishuDone] = useState<Issue[]>([]);  // fetched lazily when "show completed" is on
  const [feishuLoading, setFeishuLoading] = useState(false);
  const [resolvingFeishu, setResolvingFeishu] = useState<string | null>(null);
  const [startingFeishu, setStartingFeishu] = useState<string | null>(null);
  const didInitWeek = useRef(false);
  const feishuDoneLoaded = useRef(false);

  const username = typeof window !== "undefined" ? localStorage.getItem("appllo_username") || "" : "";
  const isAdmin = username === "sanato";

  // 优先用后端算好的 group_index(排班快照优先，新增/删除组不会让"本周"跳变)；
  // 缺失时(老后端/加载中)才回退本地公式，与后端的"查表→现算兜底"原则保持一致。
  const currentGroupIdx = (() => {
    if (currentGroupIndexFromApi !== null) return currentGroupIndexFromApi;
    if (!startDate || groups.length === 0) return -1;
    const start = new Date(startDate);
    const today = new Date();
    const weeks = Math.floor((today.getTime() - start.getTime()) / (7 * 86400000));
    return weeks % groups.length;
  })();

  // week_num → group_index 查表(来自 /oncall/week-groups)，供按历史日期归组用。
  const weekNumToGroupIdx = (() => {
    const m = new Map<number, number>();
    for (const w of weekGroups) m.set(w.week_num, w.group_index);
    return m;
  })();
  // 按日期解析归属组：优先查后端权威映射，查不到(日期早于本次上线覆盖范围，或
  // 数据还没加载完)才回退本地取模公式——与后端"查表→现算兜底"的原则保持一致。
  const resolveGroupForDate = (dateStr: string): number => {
    if (!startDate || groups.length === 0) return -1;
    const wn = weekNum(dateStr, startDate);
    const fromMap = weekNumToGroupIdx.get(wn);
    return fromMap !== undefined ? fromMap : weekGroupIndex(dateStr, startDate, groups.length);
  };

  // Most recent week_num for a group (for the current group this is the current week)
  const latestWeekForGroup = (gi: number): number | null => {
    const ws = weekStats.filter((w) => w.group_index === gi).map((w) => w.week_num);
    return ws.length ? Math.max(...ws) : null;
  };

  const load = async () => {
    try {
      const [sched, curr] = await Promise.all([getOncallSchedule(), getOncallCurrent()]);
      setGroups(sched.groups.map((g) => g.members));
      setStartDate(sched.start_date || new Date().toISOString().slice(0, 10));
      setCurrentMembers(curr.members);
      setCurrentGroupIndexFromApi(typeof curr.group_index === "number" ? curr.group_index : null);
    } catch (e: any) { setToast({ msg: e.message, type: "error" }); }
  };

  const loadTickets = async () => {
    setTicketsLoading(true);
    try {
      const [tkRes, statsRes, weekGroupsRes] = await Promise.all([
        getOncallTickets(),  // weeks=0, fetch all
        getOncallStats(),
        getOncallWeekGroups(),
      ]);
      setAllTickets(tkRes.tickets);
      setWeekStats(statsRes.weeks);
      setWeekGroups(weekGroupsRes.weeks);
    } catch (e: any) { setToast({ msg: e.message, type: "error" }); }
    finally { setTicketsLoading(false); }
  };

  const loadFeishu = async () => {
    setFeishuLoading(true);
    try {
      // All assignees' open (pending+in_progress) tickets; grouped per oncall group client-side
      const res = await getOncallFeishuTickets("open", false);
      setFeishuTickets(res.tickets);
    } catch (e: any) { setToast({ msg: e.message, type: "error" }); }
    finally { setFeishuLoading(false); }
  };

  // Feishu done tickets — loaded lazily the first time "show completed" is turned on
  const loadFeishuDone = async () => {
    try {
      const res = await getOncallFeishuTickets("done", false);
      setFeishuDone(res.tickets);
      feishuDoneLoaded.current = true;
    } catch (e: any) { setToast({ msg: e.message, type: "error" }); }
  };

  useEffect(() => { load(); }, []);
  useEffect(() => {
    if (groups.length > 0) loadFeishu();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [groups.length]);
  useEffect(() => {
    if (currentGroupIdx >= 0 && selectedGroup === -1) setSelectedGroup(currentGroupIdx);
  }, [currentGroupIdx, selectedGroup]);
  useEffect(() => {
    if (groups.length > 0) loadTickets();
  }, [groups.length]);
  // Default the week filter to the selected group's current/most-recent week (not "All")
  useEffect(() => {
    if (!didInitWeek.current && selectedGroup >= 0 && weekStats.length > 0) {
      const lw = latestWeekForGroup(selectedGroup);
      if (lw !== null) { setSelectedWeek(lw); didInitWeek.current = true; }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedGroup, weekStats.length]);
  // Lazily fetch Feishu done tickets the first time "show completed" is enabled
  useEffect(() => {
    if (showResolved && !feishuDoneLoaded.current && groups.length > 0) loadFeishuDone();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showResolved, groups.length]);

  // Filter tickets based on selection
  const filteredTickets = allTickets.filter((tk) => {
    if (selectedGroup < 0 || !startDate || groups.length === 0) return false;
    const gi = resolveGroupForDate(tk.escalated_at);
    if (gi !== selectedGroup) return false;
    if (selectedWeek !== null) {
      return weekNum(tk.escalated_at, startDate) === selectedWeek;
    }
    return true;
  });
  const inProgressTickets = filteredTickets.filter((tk) => tk.escalation_status !== "resolved");
  const resolvedTickets = filteredTickets.filter((tk) => tk.escalation_status === "resolved");

  // Feishu tickets for the selected group + week, split into open / done.
  // Membership match on assignee email; week match on creation date (oncall is week-based).
  const feishuForGroupWeek = (list: Issue[]): Issue[] => {
    if (selectedGroup < 0) return [];
    const groupEmails = new Set((groups[selectedGroup] || []).map((e) => e.toLowerCase()));
    return list.filter((tk) => {
      if (!(tk.assignee_emails || []).some((e) => groupEmails.has(e))) return false;
      if (selectedWeek !== null) {
        if (!tk.created_at_ms) return false;
        if (weekNum(new Date(tk.created_at_ms).toISOString(), startDate) !== selectedWeek) return false;
      }
      return true;
    });
  };
  const openFeishuTickets = feishuForGroupWeek(feishuTickets);
  const doneFeishuTickets = feishuForGroupWeek(feishuDone);

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

  const handleResolveFeishu = async (recordId: string) => {
    setResolvingFeishu(recordId);
    try {
      await resolveFeishuTicket(recordId);
      // We only show open tickets → drop it from the list once marked done
      setFeishuTickets((prev) => prev.filter((tk) => tk.record_id !== recordId));
      setToast({ msg: t("工单已标记完成"), type: "success" });
    } catch (e: any) { setToast({ msg: e.message, type: "error" }); }
    finally { setResolvingFeishu(null); }
  };

  // "开始处理" on a pending Feishu ticket → kick off AI analysis (the worker also
  // sets 开始处理=true on the bitable, moving it to in-progress).
  const handleStartFeishu = async (recordId: string) => {
    setStartingFeishu(recordId);
    try {
      await createTask(recordId, undefined, username);
      setFeishuTickets((prev) => prev.map((tk) =>
        tk.record_id === recordId ? { ...tk, feishu_status: "in_progress" } : tk
      ));
      setToast({ msg: t("已开始处理，AI 分析中"), type: "success" });
    } catch (e: any) { setToast({ msg: e.message, type: "error" }); }
    finally { setStartingFeishu(null); }
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

  // Escalated (in-progress) + Feishu (open) workload for a group's CURRENT/most-recent week
  // → shown next to the group; matches the default week view when you click in.
  const groupTicketCount = (gi: number) => {
    const wk = latestWeekForGroup(gi);
    const escalated = allTickets.filter((tk) =>
      resolveGroupForDate(tk.escalated_at) === gi
      && tk.escalation_status !== "resolved"
      && (wk === null || weekNum(tk.escalated_at, startDate) === wk)
    ).length;
    const groupEmails = new Set((groups[gi] || []).map((e) => e.toLowerCase()));
    const feishu = feishuTickets.filter((tk) =>
      (tk.assignee_emails || []).some((e) => groupEmails.has(e))
      && (wk === null || (!!tk.created_at_ms && weekNum(new Date(tk.created_at_ms).toISOString(), startDate) === wk))
    ).length;
    return escalated + feishu;
  };

  // Click a week in stats → switch to tickets tab filtered to that week
  const selectWeekFromStats = (ws: OncallWeekStat) => {
    setSelectedGroup(ws.group_index);
    setSelectedWeek(ws.week_num);
    setTab("tickets");
  };

  // ---- Card renderers (reused across the in-progress columns and the completed column) ----
  const renderEscalatedCard = (tk: EscalatedTicket) => {
    const isResolved = tk.escalation_status === "resolved";
    return (
      <div key={tk.record_id} className="rounded-xl p-4"
        style={{ background: S.overlay, border: `1px solid ${S.border}`, opacity: isResolved ? 0.6 : 1 }}>
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-2">
            <SourceBadge source={tk.source} t={t} />
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
          <span className="rounded-full px-2 py-0.5 text-[10px]"
            style={isResolved
              ? { background: "rgba(34,197,94,0.18)", color: "#15803D", border: "1px solid rgba(34,197,94,0.4)", fontWeight: 700 }
              : { background: "rgba(234,179,8,0.12)", color: "#B45309", border: "1px solid rgba(234,179,8,0.25)", fontWeight: 500 }}>
            {isResolved ? `✓ ${t("已完成")}` : t("进行中")}
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
          {isResolved && tk.escalation_resolved_at && (
            <span style={{ color: "#16A34A" }}>{t("完成于")} {formatTime(tk.escalation_resolved_at)}</span>
          )}
        </div>

        <div className="flex items-center gap-2 pt-2" style={{ borderTop: `1px solid ${S.border}` }}>
          <a href={`/tracking?detail=${tk.record_id}`}
            className="rounded-lg px-3 py-1.5 text-[11px] font-medium"
            style={{ color: "#2563EB", background: "rgba(37,99,235,0.06)", border: "1px solid rgba(37,99,235,0.15)" }}>
            {t("查看详情")}
          </a>
          {tk.escalation_share_link && (
            <a href={tk.escalation_share_link} target="_blank" rel="noreferrer"
              className="rounded-lg px-3 py-1.5 text-[11px] font-medium flex items-center gap-1"
              style={{ color: "#FFFFFF", background: "#EA580C", border: "1px solid #C2410C", textDecoration: "none" }}>
              <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M18 9v3m0 0v3m0-3h3m-3 0h-3m-2-5a4 4 0 11-8 0 4 4 0 018 0zM3 20a6 6 0 0112 0v1H3v-1z" />
              </svg>
              {t("加入群")}
            </a>
          )}
          {!isResolved && (
            <button onClick={() => handleResolve(tk.record_id)}
              disabled={resolving === tk.record_id}
              className="rounded-lg px-3 py-1.5 text-[11px] font-medium disabled:opacity-50"
              style={{ background: "rgba(34,197,94,0.1)", color: "#16A34A", border: "1px solid rgba(34,197,94,0.25)" }}>
              {resolving === tk.record_id ? t("处理中...") : t("标记完成")}
            </button>
          )}
        </div>
      </div>
    );
  };

  const renderFeishuCard = (tk: Issue) => {
    const isDone = tk.feishu_status === "done";
    const isPending = tk.feishu_status === "pending";
    return (
      <div key={tk.record_id} className="rounded-xl p-4"
        style={{ background: S.overlay, border: `1px solid ${S.border}`, opacity: isDone ? 0.6 : 1 }}>
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-2">
            <SourceBadge source={tk.source} t={t} />
            {tk.priority && (
              <span className="rounded-full px-2 py-0.5 text-[10px] font-medium"
                style={tk.priority === "H"
                  ? { background: "rgba(239,68,68,0.1)", color: "#DC2626", border: "1px solid rgba(239,68,68,0.2)" }
                  : { background: S.surface, color: S.text2, border: `1px solid ${S.border}` }}>
                {tk.priority === "H" ? t("高优先级") : tk.priority}
              </span>
            )}
            {tk.zendesk_id && (
              <span className="text-[10px] font-mono" style={{ color: S.text3 }}>#{tk.zendesk_id}</span>
            )}
          </div>
          <span className="rounded-full px-2 py-0.5 text-[10px]"
            style={isDone
              ? { background: "rgba(34,197,94,0.18)", color: "#15803D", border: "1px solid rgba(34,197,94,0.4)", fontWeight: 700 }
              : tk.feishu_status === "in_progress"
                ? { background: "rgba(234,179,8,0.12)", color: "#B45309", border: "1px solid rgba(234,179,8,0.25)", fontWeight: 500 }
                : { background: S.surface, color: S.text2, border: `1px solid ${S.border}`, fontWeight: 500 }}>
            {isDone ? `✓ ${t("已完成")}` : tk.feishu_status === "in_progress" ? t("处理中") : t("待处理")}
          </span>
        </div>

        <p className="text-xs leading-relaxed mb-2" style={{ color: S.text1 }}>
          {truncate(tk.description, 150)}
        </p>

        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] mb-2" style={{ color: S.text3 }}>
          {tk.assignee && <span>{t("指派人")}: {tk.assignee}</span>}
          {tk.created_at_ms > 0 && <span>{formatTime(new Date(tk.created_at_ms).toISOString())}</span>}
        </div>

        <div className="flex items-center gap-2 pt-2" style={{ borderTop: `1px solid ${S.border}` }}>
          {tk.feishu_link && (
            <a href={tk.feishu_link} target="_blank" rel="noreferrer"
              className="rounded-lg px-3 py-1.5 text-[11px] font-medium"
              style={{ color: "#2563EB", background: "rgba(37,99,235,0.06)", border: "1px solid rgba(37,99,235,0.15)" }}>
              {t("去飞书")}
            </a>
          )}
          {isPending && (
            <button onClick={() => handleStartFeishu(tk.record_id)}
              disabled={startingFeishu === tk.record_id}
              className="rounded-lg px-3 py-1.5 text-[11px] font-medium disabled:opacity-50"
              style={{ background: "rgba(37,99,235,0.1)", color: "#2563EB", border: "1px solid rgba(37,99,235,0.25)" }}>
              {startingFeishu === tk.record_id ? t("处理中...") : t("开始处理")}
            </button>
          )}
          {!isDone && (
            <button onClick={() => handleResolveFeishu(tk.record_id)}
              disabled={resolvingFeishu === tk.record_id}
              className="rounded-lg px-3 py-1.5 text-[11px] font-medium disabled:opacity-50"
              style={{ background: "rgba(34,197,94,0.1)", color: "#16A34A", border: "1px solid rgba(34,197,94,0.25)" }}>
              {resolvingFeishu === tk.record_id ? t("处理中...") : t("标记完成")}
            </button>
          )}
        </div>
      </div>
    );
  };

  // =========================================================================
  // EDIT MODE
  // =========================================================================
  if (editing) {
    return (
      <div className="min-h-full">
        <header className="sticky top-0 z-10 backdrop-blur-md j-rise"
          style={{ background: "var(--j-header)", borderBottom: `1px solid ${S.border}` }}>
          <div className="flex items-center justify-between px-6 py-3">
            <div>
              <h1 className="text-base font-semibold" style={{ color: S.text1 }}>{t("值班管理")}</h1>
              <p className="text-xs mt-0.5" style={{ color: S.text3 }}>{t("编辑排班中")}</p>
            </div>
            <div className="flex items-center gap-2">
              <button onClick={save} disabled={saving}
                className="rounded-lg px-4 py-1.5 text-sm font-semibold disabled:opacity-50"
                style={{ background: S.accent, color: "#FFFFFF" }}>
                {saving ? t("保存中...") : t("保存")}
              </button>
              <button onClick={() => { setEditing(false); load(); }}
                className="rounded-lg px-3 py-1.5 text-sm font-medium"
                style={{ border: `1px solid ${S.border}`, color: S.text2 }}>{t("取消")}</button>
            </div>
          </div>
        </header>
        <div className="mx-auto max-w-3xl px-6 py-6 space-y-5">
          <section className="rounded-xl p-5" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
            <h2 className="mb-3 text-sm font-semibold" style={{ color: S.text1 }}>{t("轮换起始日期")}</h2>
            <input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)}
              className="rounded-lg px-3 py-2 text-sm font-sans outline-none" style={inputStyle} />
          </section>
          <section className="rounded-xl p-5" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-sm font-semibold" style={{ color: S.text1 }}>{t("值班分组")}</h2>
              <button onClick={addGroup} className="rounded-lg px-3 py-1 text-xs font-medium"
                style={{ background: S.accentBg, color: S.accent, border: "1px solid rgba(14,124,134,0.25)" }}>
                {t("添加分组")}</button>
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

  // =========================================================================
  // VIEW MODE
  // =========================================================================
  return (
    <div className="min-h-full">
      <header className="sticky top-0 z-10 backdrop-blur-md j-rise"
        style={{ background: "var(--j-header)", borderBottom: `1px solid ${S.border}` }}>
        <div className="flex items-center justify-between px-6 py-3">
          <div className="flex items-center gap-4">
            <div>
              <h1 className="text-base font-semibold" style={{ color: S.text1 }}>{t("值班管理")}</h1>
              <p className="text-xs mt-0.5" style={{ color: S.text3 }}>{t("每周轮换，自动通知值班工程师")}</p>
            </div>
            {/* Tab switcher */}
            <div className="flex rounded-lg overflow-hidden" style={{ border: `1px solid ${S.border}` }}>
              {(["tickets", "stats"] as const).map((k) => (
                <button key={k} onClick={() => setTab(k)}
                  className="px-3 py-1 text-xs font-medium transition-colors"
                  style={tab === k
                    ? { background: S.text1, color: "#fff" }
                    : { background: "transparent", color: S.text2 }}>
                  {k === "tickets" ? t("工单") : t("周报")}
                </button>
              ))}
            </div>
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

      {/* ================= STATS TAB ================= */}
      {tab === "stats" && (
        <div className="mx-auto max-w-5xl px-6 py-6">
          <h2 className="text-sm font-semibold mb-4" style={{ color: S.text1 }}>{t("每周值班统计")}</h2>
          {weekStats.length === 0 ? (
            <p className="py-12 text-center text-sm" style={{ color: S.text3 }}>{t("暂无数据")}</p>
          ) : (
            <div className="rounded-xl overflow-hidden" style={{ border: `1px solid ${S.border}` }}>
              <table className="w-full text-xs">
                <thead>
                  <tr style={{ background: S.surface }}>
                    <th className="text-left px-4 py-2.5 font-semibold" style={{ color: S.text3 }}>{t("周")}</th>
                    <th className="text-left px-4 py-2.5 font-semibold" style={{ color: S.text3 }}>{t("日期范围")}</th>
                    <th className="text-left px-4 py-2.5 font-semibold" style={{ color: S.text3 }}>{t("值班人")}</th>
                    <th className="text-center px-4 py-2.5 font-semibold" style={{ color: S.text3 }}>{t("总工单")}</th>
                    <th className="text-center px-4 py-2.5 font-semibold" style={{ color: "#B45309" }}>{t("进行中")}</th>
                    <th className="text-center px-4 py-2.5 font-semibold" style={{ color: "#16A34A" }}>{t("已完成")}</th>
                  </tr>
                </thead>
                <tbody>
                  {weekStats.map((ws) => (
                    <tr key={ws.week_num}
                      className="cursor-pointer transition-colors"
                      onClick={() => selectWeekFromStats(ws)}
                      style={{
                        background: ws.is_current ? "rgba(34,197,94,0.04)" : S.overlay,
                        borderTop: `1px solid ${S.border}`,
                      }}
                      onMouseEnter={(e) => e.currentTarget.style.background = S.hover}
                      onMouseLeave={(e) => e.currentTarget.style.background = ws.is_current ? "rgba(34,197,94,0.04)" : S.overlay}>
                      <td className="px-4 py-2.5">
                        <div className="flex items-center gap-1.5">
                          <span style={{ color: S.text1 }}>{t("第")} {ws.group_index + 1} {t("组")}</span>
                          {ws.is_current && (
                            <span className="rounded-full px-1.5 py-0.5 text-[8px] font-bold"
                              style={{ background: "rgba(34,197,94,0.15)", color: "#16A34A" }}>{t("本周")}</span>
                          )}
                        </div>
                      </td>
                      <td className="px-4 py-2.5 font-mono" style={{ color: S.text2 }}>
                        {formatDate(ws.week_start)} ~ {formatDate(ws.week_end)}
                      </td>
                      <td className="px-4 py-2.5" style={{ color: S.text2 }}>
                        {ws.members.map((m) => m.split("@")[0]).join(", ")}
                      </td>
                      <td className="text-center px-4 py-2.5 font-bold" style={{ color: ws.total > 0 ? S.text1 : S.text3 }}>
                        {ws.total}
                      </td>
                      <td className="text-center px-4 py-2.5 font-bold" style={{ color: ws.in_progress > 0 ? "#B45309" : S.text3 }}>
                        {ws.in_progress}
                      </td>
                      <td className="text-center px-4 py-2.5 font-bold" style={{ color: ws.resolved > 0 ? "#16A34A" : S.text3 }}>
                        {ws.resolved}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* ================= TICKETS TAB ================= */}
      {tab === "tickets" && (
        <div className="flex" style={{ minHeight: "calc(100vh - 52px)" }}>
          {/* Left sidebar */}
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
                  onClick={() => { setSelectedGroup(gi); setSelectedWeek(latestWeekForGroup(gi)); }}
                  className="w-full rounded-lg px-3 py-2.5 text-left transition-all"
                  style={{
                    background: isSelected ? (isCurrent ? "rgba(34,197,94,0.12)" : S.overlay) : "transparent",
                    border: isSelected ? `1.5px solid ${isCurrent ? "rgba(34,197,94,0.4)" : S.border}` : "1.5px solid transparent",
                  }}>
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-xs font-semibold" style={{ color: isSelected ? S.text1 : S.text2 }}>
                      {t("第")} {gi + 1} {t("组")}
                    </span>
                    <div className="flex items-center gap-1">
                      {isCurrent && (
                        <span className="rounded-full px-1.5 py-0.5 text-[8px] font-bold"
                          style={{ background: "rgba(34,197,94,0.15)", color: "#16A34A" }}>{t("本周")}</span>
                      )}
                      {count > 0 && (
                        <span className="rounded-full min-w-[16px] h-4 flex items-center justify-center text-[9px] font-bold"
                          style={{ background: "rgba(234,179,8,0.18)", color: "#B45309" }}>{count}</span>
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
            {startDate && (
              <p className="px-2 pt-3 text-[9px]" style={{ color: S.text3, borderTop: `1px solid ${S.border}` }}>
                {t("起始")}: {startDate}
              </p>
            )}
          </aside>

          {/* Right content */}
          <main className="flex-1 overflow-y-auto p-6">
            {selectedGroup < 0 ? (
              <div className="flex items-center justify-center h-full">
                <p className="text-sm" style={{ color: S.text3 }}>{t("点击左侧分组查看工单")}</p>
              </div>
            ) : (
              <>
                {/* Header */}
                <div className="flex items-center justify-between mb-3">
                  <div className="flex items-center gap-3">
                    <h2 className="text-sm font-semibold" style={{ color: S.text1 }}>
                      {t("第")} {selectedGroup + 1} {t("组")} — {t("工单总览")}
                    </h2>
                    {selectedGroup === currentGroupIdx && (
                      <span className="rounded-full px-2 py-0.5 text-[9px] font-bold"
                        style={{ background: "rgba(34,197,94,0.15)", color: "#16A34A" }}>{t("本周")}</span>
                    )}
                  </div>
                  <div className="flex items-center gap-3 text-xs">
                    <span style={{ color: "#2563EB" }}>{t("升级")} {inProgressTickets.length}</span>
                    <span style={{ color: "#B45309" }}>{t("飞书")} {openFeishuTickets.length}</span>
                    <button onClick={() => setShowResolved((v) => !v)}
                      className="rounded-lg px-2.5 py-1 text-[11px] font-medium transition-colors"
                      style={showResolved
                        ? { background: S.text1, color: "#fff" }
                        : { background: S.surface, color: S.text2, border: `1px solid ${S.border}` }}>
                      {t("显示已完成")}
                    </button>
                  </div>
                </div>

                {/* Week filter chips */}
                {(() => {
                  const groupWeeks = weekStats.filter((ws) => ws.group_index === selectedGroup);
                  if (groupWeeks.length <= 1) return null;
                  return (
                    <div className="flex flex-wrap gap-1.5 mb-4">
                      <button onClick={() => setSelectedWeek(null)}
                        className="rounded-lg px-2.5 py-1 text-[11px] font-medium transition-colors"
                        style={selectedWeek === null
                          ? { background: S.text1, color: "#fff" }
                          : { background: S.surface, color: S.text2, border: `1px solid ${S.border}` }}>
                        {t("全部")}
                      </button>
                      {groupWeeks.map((ws) => (
                        <button key={ws.week_num}
                          onClick={() => setSelectedWeek(ws.week_num)}
                          className="rounded-lg px-2.5 py-1 text-[11px] font-medium transition-colors"
                          style={selectedWeek === ws.week_num
                            ? { background: S.text1, color: "#fff" }
                            : { background: S.surface, color: S.text2, border: `1px solid ${S.border}` }}>
                          {formatDate(ws.week_start)}~{formatDate(ws.week_end)}
                          {ws.is_current && ` (${t("本周")})`}
                          {ws.total > 0 && ` · ${ws.total}`}
                        </button>
                      ))}
                    </div>
                  );
                })()}

                {/* Members bar */}
                <div className="flex flex-wrap gap-1.5 mb-4">
                  {(groups[selectedGroup] || []).map((m) => (
                    <span key={m} className="rounded-lg px-2.5 py-1 text-[11px] font-medium"
                      style={{
                        background: selectedGroup === currentGroupIdx ? "rgba(34,197,94,0.08)" : S.surface,
                        color: selectedGroup === currentGroupIdx ? "#16A34A" : S.text2,
                        border: `1px solid ${selectedGroup === currentGroupIdx ? "rgba(34,197,94,0.2)" : S.border}`,
                      }}>{m}</span>
                  ))}
                </div>

                {/* ===== Columns: Escalated | Feishu [ | Completed when shown ] ===== */}
                <div className={`grid grid-cols-1 gap-5 items-start ${showResolved ? "xl:grid-cols-3" : "xl:grid-cols-2"}`}>
                  {/* Column 1: Escalated in-progress */}
                  <div>
                    <h3 className="text-xs font-semibold mb-2 flex items-center gap-2" style={{ color: S.text2 }}>
                      {t("升级工单")}
                      <span className="text-[10px] font-normal" style={{ color: S.text3 }}>{inProgressTickets.length}</span>
                    </h3>
                    {ticketsLoading ? (
                      <p className="py-8 text-center text-xs" style={{ color: S.text3 }}>{t("加载中...")}</p>
                    ) : inProgressTickets.length === 0 ? (
                      <div className="rounded-xl py-8 text-center" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
                        <p className="text-xs" style={{ color: S.text3 }}>{t("暂无转交工单")}</p>
                      </div>
                    ) : (
                      <div className="space-y-3">{inProgressTickets.map(renderEscalatedCard)}</div>
                    )}
                  </div>

                  {/* Column 2: Feishu in-progress (this group's assignees) */}
                  <div>
                    <h3 className="text-xs font-semibold mb-2 flex items-center gap-2" style={{ color: S.text2 }}>
                      {t("飞书在处理")}
                      <span className="text-[10px] font-normal" style={{ color: S.text3 }}>{openFeishuTickets.length}</span>
                    </h3>
                    {feishuLoading ? (
                      <p className="py-8 text-center text-xs" style={{ color: S.text3 }}>{t("加载中...")}</p>
                    ) : openFeishuTickets.length === 0 ? (
                      <div className="rounded-xl py-8 text-center" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
                        <p className="text-xs" style={{ color: S.text3 }}>{t("暂无飞书工单")}</p>
                      </div>
                    ) : (
                      <div className="space-y-3">{openFeishuTickets.map(renderFeishuCard)}</div>
                    )}
                  </div>

                  {/* Column 3: Completed — Apollo resolved first, then Feishu done (only when shown) */}
                  {showResolved && (
                    <div>
                      <h3 className="text-xs font-semibold mb-2 flex items-center gap-2" style={{ color: S.text2 }}>
                        {t("已完成")}
                        <span className="text-[10px] font-normal" style={{ color: S.text3 }}>{resolvedTickets.length + doneFeishuTickets.length}</span>
                      </h3>
                      {(resolvedTickets.length + doneFeishuTickets.length) === 0 ? (
                        <div className="rounded-xl py-8 text-center" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
                          <p className="text-xs" style={{ color: S.text3 }}>{t("暂无已完成工单")}</p>
                        </div>
                      ) : (
                        <div className="space-y-3">
                          {resolvedTickets.map(renderEscalatedCard)}
                          {doneFeishuTickets.map(renderFeishuCard)}
                        </div>
                      )}
                    </div>
                  )}
                </div>{/* /columns grid */}
              </>
            )}
          </main>
        </div>
      )}

      {toast && <Toast msg={toast.msg} type={toast.type} onClose={() => setToast(null)} />}
    </div>
  );
}
