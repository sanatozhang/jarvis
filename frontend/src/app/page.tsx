"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import { useT, useLang } from "@/lib/i18n";
import {
  fetchPendingIssues,
  refreshIssuesCache,
  fetchCompleted,
  fetchInProgress,
  fetchInaccurate,
  markInaccurate,
  createTask,
  deleteIssue,
  fetchIssueDetail,
  fetchIssueAnalyses,
  loginUser,
  subscribeTaskProgress,
  fetchTaskResult,
  formatLocalTime,
  type Issue,
  type TaskProgress,
  type AnalysisResult,
  type LocalIssueItem,
  type PaginatedResponse,
} from "@/lib/api";

// ── URL helpers ──────────────────────────────────────────────
function getUrlParam(key: string): string {
  if (typeof window === "undefined") return "";
  return new URLSearchParams(window.location.search).get(key) || "";
}
function setUrlParam(key: string, val: string) {
  const url = new URL(window.location.href);
  val ? url.searchParams.set(key, val) : url.searchParams.delete(key);
  window.history.replaceState({}, "", url.toString());
}

// ── Shared design tokens ─────────────────────────────────────
const S = {
  surface:  "#F8F9FA",
  overlay:  "#FFFFFF",
  hover:    "#EEF0F2",
  border:   "rgba(0,0,0,0.08)",
  borderSm: "rgba(0,0,0,0.04)",
  accent:   "#B8922E",
  accentBg: "rgba(184,146,46,0.06)",
  text1:    "#111827",
  text2:    "#6B7280",
  text3:    "#9CA3AF",
};

// ── Small components ─────────────────────────────────────────
function PriorityBadge({ p }: { p: string }) {
  const t = useT();
  return p === "H" ? (
    <span className="inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-semibold"
      style={{ background: "rgba(239,68,68,0.15)", color: "#DC2626", border: "1px solid rgba(239,68,68,0.25)" }}>
      {t("高")}
    </span>
  ) : (
    <span className="inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium"
      style={{ background: "rgba(0,0,0,0.04)", color: "#6B7280", border: "1px solid rgba(0,0,0,0.08)" }}>
      {t("低")}
    </span>
  );
}

function SourceBadge({ source, linearUrl }: { source?: string; linearUrl?: string }) {
  const t = useT();
  const cfg: Record<string, { label: string; bg: string; color: string; border: string }> = {
    feishu:  { label: t("飞书"),     bg: "rgba(96,165,250,0.12)", color: "#2563EB", border: "rgba(96,165,250,0.25)" },
    linear:  { label: "Linear",      bg: "rgba(167,139,250,0.12)", color: "#C4B5FD", border: "rgba(167,139,250,0.25)" },
    api:     { label: "API",         bg: "rgba(52,211,153,0.12)", color: "#6EE7B7", border: "rgba(52,211,153,0.25)" },
    local:   { label: t("手动提交"), bg: "rgba(251,146,60,0.12)",  color: "#FCA87A", border: "rgba(251,146,60,0.25)" },
  };
  const s = source || "feishu";
  const c = cfg[s] || cfg.feishu;
  const badge = (
    <span className="inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium"
      style={{ background: c.bg, color: c.color, border: `1px solid ${c.border}` }}>
      {c.label}
    </span>
  );
  if (s === "linear" && linearUrl) {
    return <a href={linearUrl} target="_blank" onClick={(e) => e.stopPropagation()} className="hover:opacity-80">{badge}</a>;
  }
  return badge;
}

function ConfBadge({ c }: { c: string }) {
  const m: Record<string, { bg: string; color: string; border: string }> = {
    high:   { bg: "rgba(34,197,94,0.12)",   color: "#16A34A", border: "rgba(34,197,94,0.25)" },
    medium: { bg: "rgba(234,179,8,0.12)",   color: "#FCD34D", border: "rgba(234,179,8,0.25)" },
    low:    { bg: "rgba(239,68,68,0.12)",   color: "#DC2626", border: "rgba(239,68,68,0.25)" },
  };
  const s = m[c] || m.low;
  return (
    <span className="inline-flex rounded-full px-2 py-0.5 text-[10px] font-medium"
      style={{ background: s.bg, color: s.color, border: `1px solid ${s.border}` }}>
      {c}
    </span>
  );
}

function LocalStatusBadge({ item }: { item: LocalIssueItem }) {
  const t = useT();
  const task = item.task;
  const analysis = item.analysis;
  if (task && !["done", "failed"].includes(task.status)) {
    const labels: Record<string, string> = {
      queued: t("排队中"), downloading: t("下载中"),
      decrypting: t("解密中"), extracting: t("提取中"), analyzing: t("分析中"),
    };
    return (
      <span className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium"
        style={{ background: "rgba(96,165,250,0.12)", color: "#2563EB", border: "1px solid rgba(96,165,250,0.25)" }}>
        <span className="h-1.5 w-1.5 animate-pulse rounded-full" style={{ background: "#2563EB" }} />
        {labels[task.status] || task.status}
      </span>
    );
  }
  if ((item.local_status === "done" || analysis) && analysis) {
    const ruleMatched = analysis.rule_type && analysis.rule_type !== "general";
    return (
      <span className="inline-flex items-center gap-1">
        <span className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium"
          style={{ background: "rgba(34,197,94,0.12)", color: "#16A34A", border: "1px solid rgba(34,197,94,0.25)" }}>
          <svg className="h-2.5 w-2.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
          </svg>
          {t("成功")}
        </span>
        {ruleMatched && (
          <span className="inline-flex rounded-full px-1.5 py-0.5 text-[9px] font-bold"
            style={{ background: "rgba(184,146,46,0.15)", color: "#B8922E", border: "1px solid rgba(184,146,46,0.3)" }}>
            100%
          </span>
        )}
      </span>
    );
  }
  if (item.local_status === "done")
    return (
      <span className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium"
        style={{ background: "rgba(34,197,94,0.12)", color: "#16A34A", border: "1px solid rgba(34,197,94,0.25)" }}>
        <svg className="h-2.5 w-2.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
        </svg>
        {t("成功")}
      </span>
    );
  if (item.local_status === "failed")
    return (
      <span className="inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium"
        style={{ background: "rgba(239,68,68,0.12)", color: "#DC2626", border: "1px solid rgba(239,68,68,0.25)" }}>
        {t("分析失败")}
      </span>
    );
  if (item.local_status === "inaccurate")
    return (
      <span className="inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium"
        style={{ background: "rgba(239,68,68,0.12)", color: "#DC2626", border: "1px solid rgba(239,68,68,0.25)" }}>
        {t("分析不准确")}
      </span>
    );
  return <span style={{ color: S.text3, fontSize: "12px" }}>—</span>;
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

function Pagination({ page, totalPages, onChange }: { page: number; totalPages: number; onChange: (p: number) => void }) {
  if (totalPages <= 1) return null;
  const t = useT();
  return (
    <div className="mt-4 flex items-center justify-center gap-2">
      <button disabled={page <= 1} onClick={() => onChange(page - 1)}
        className="rounded-lg px-3 py-1.5 text-xs font-medium transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
        style={{ border: `1px solid ${S.border}`, color: S.text2 }}>
        {t("上一页")}
      </button>
      <span className="text-xs tabular-nums" style={{ color: S.text3 }}>{page} / {totalPages}</span>
      <button disabled={page >= totalPages} onClick={() => onChange(page + 1)}
        className="rounded-lg px-3 py-1.5 text-xs font-medium transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
        style={{ border: `1px solid ${S.border}`, color: S.text2 }}>
        {t("下一页")}
      </button>
    </div>
  );
}

function IndeterminateCheckbox({ checked, indeterminate, onChange }: {
  checked: boolean; indeterminate: boolean;
  onChange: (e: React.ChangeEvent<HTMLInputElement>) => void;
}) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => { if (ref.current) ref.current.indeterminate = indeterminate; }, [indeterminate]);
  return <input ref={ref} type="checkbox" className="rounded" style={{ accentColor: S.accent }} checked={checked} onChange={onChange} />;
}

// ── Btn helpers ───────────────────────────────────────────────
const btnPrimary = "rounded-lg px-3 py-1.5 text-sm font-semibold transition-colors";
const btnGhost = "rounded-lg px-3 py-1.5 text-sm font-medium transition-colors";

// ── Types ─────────────────────────────────────────────────────
type Tab = "pending" | "in_progress" | "done" | "inaccurate";
const PAGE_SIZE = 20;

export default function HomePage() {
  const t = useT();
  const [pendingData, setPendingData] = useState<PaginatedResponse<Issue> | null>(null);
  const [ipData, setIpData] = useState<PaginatedResponse<LocalIssueItem> | null>(null);
  const [doneData, setDoneData] = useState<PaginatedResponse<LocalIssueItem> | null>(null);

  const [inaccurateData, setInaccurateData] = useState<PaginatedResponse<LocalIssueItem> | null>(null);

  const [pendingPage, setPendingPage] = useState(1);
  const [ipPage, setIpPage] = useState(1);
  const [donePage, setDonePage] = useState(1);
  const [inaccuratePage, setInaccuratePage] = useState(1);

  const [loading, setLoading] = useState(true);
  const [pendingLoading, setPendingLoading] = useState(true);
  const [error, setError] = useState("");

  const [activeTasks, setActiveTasks] = useState<Record<string, TaskProgress>>({});
  const [activeResults, setActiveResults] = useState<Record<string, AnalysisResult>>({});

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [detailId, setDetailId] = useState<string | null>(null);
  const [directDetail, setDirectDetail] = useState<LocalIssueItem | null>(null);
  const siteLang = useLang();
  const [lang, setLang] = useState<"cn" | "en">(siteLang);
  const [detailTab, setDetailTab] = useState<Tab>("pending");
  const [toast, setToast] = useState("");
  const [tab, setTab] = useState<Tab>("done");

  // Follow-up state
  const [issueAnalyses, setIssueAnalyses] = useState<Record<string, AnalysisResult[]>>({});
  const [followupText, setFollowupText] = useState("");
  const [followupSubmitting, setFollowupSubmitting] = useState(false);

  useEffect(() => {
    const urlTab = new URLSearchParams(window.location.search).get("tab");
    if (urlTab === "in_progress" || urlTab === "done" || urlTab === "inaccurate") setTab(urlTab);
    const urlDetail = getUrlParam("detail");
    if (urlDetail) {
      setDetailId(urlDetail);
      const urlDetailTab = getUrlParam("dtab");
      if (urlDetailTab === "in_progress" || urlDetailTab === "done" || urlDetailTab === "inaccurate") setDetailTab(urlDetailTab);
      else if (urlDetailTab === "pending") setDetailTab("pending");
    }
  }, []);

  const [username, setUsername] = useState<string | null>(null);
  const [usernameInput, setUsernameInput] = useState("");
  const [showUsernameEdit, setShowUsernameEdit] = useState(false);
  const [showUsernameSetup, setShowUsernameSetup] = useState(false);

  const [assignee, setAssignee] = useState<string | null>(null);
  const [assigneeInput, setAssigneeInput] = useState("");
  const [showAssigneeEdit, setShowAssigneeEdit] = useState(false);

  useEffect(() => {
    const savedName = typeof window !== "undefined" ? localStorage.getItem("appllo_username") || "" : "";
    if (savedName) { setUsername(savedName); setUsernameInput(savedName); }
    else { setUsername(""); setShowUsernameSetup(true); }
    const fromUrl = getUrlParam("assignee");
    const fromStorage = typeof window !== "undefined" ? localStorage.getItem("appllo_assignee") || "" : "";
    const a = fromUrl || fromStorage;
    setAssignee(a);
    setAssigneeInput(a);
    if (a) { setUrlParam("assignee", a); localStorage.setItem("appllo_assignee", a); }
  }, []);

  const saveUsername = async (name: string) => {
    const v = name.trim();
    if (!v) return;
    setUsername(v); setUsernameInput(v);
    localStorage.setItem("appllo_username", v);
    setShowUsernameSetup(false); setShowUsernameEdit(false);
    try {
      const user = await loginUser(v);
      localStorage.setItem("appllo_role", user.role);
      if (user.feishu_email) localStorage.setItem("appllo_feishu_email", user.feishu_email);
    } catch {}
  };

  const applyAssignee = () => {
    const v = assigneeInput.trim();
    setAssignee(v); setUrlParam("assignee", v);
    if (v) { localStorage.setItem("appllo_assignee", v); } else { localStorage.removeItem("appllo_assignee"); }
    setShowAssigneeEdit(false); setPendingPage(1);
  };
  const clearAssignee = () => {
    setAssignee(""); setAssigneeInput(""); setUrlParam("assignee", "");
    localStorage.removeItem("appllo_assignee"); setShowAssigneeEdit(false); setPendingPage(1);
  };

  const loadPending = useCallback(async (page: number) => {
    if (assignee === null) return;
    setPendingLoading(true);
    try { const d = await fetchPendingIssues(assignee || undefined, page, PAGE_SIZE); setPendingData(d); }
    catch (e: any) { setError(e.message); }
    finally { setPendingLoading(false); }
  }, [assignee]);

  const loadInProgress = useCallback(async (page: number) => {
    try { setIpData(await fetchInProgress(page, PAGE_SIZE)); } catch (e: any) { setError(e.message); }
  }, []);

  const loadDone = useCallback(async (page: number) => {
    try { setDoneData(await fetchCompleted(page, PAGE_SIZE)); } catch (e: any) { setError(e.message); }
  }, []);

  const loadInaccurate = useCallback(async (page: number) => {
    try { setInaccurateData(await fetchInaccurate(page, PAGE_SIZE)); } catch (e: any) { setError(e.message); }
  }, []);

  const loadAll = useCallback(async () => {
    if (assignee === null) return;
    setLoading(true); setError("");
    // 先加载本地数据（快），加载完立即显示页面
    await Promise.all([loadInProgress(ipPage), loadDone(donePage), loadInaccurate(inaccuratePage)]);
    setLoading(false);
    // 再异步加载飞书数据（慢），完成后自动刷新待处理 tab
    loadPending(pendingPage);
  }, [assignee, loadPending, loadInProgress, loadDone, loadInaccurate, pendingPage, ipPage, donePage, inaccuratePage]);

  useEffect(() => { loadAll(); }, [loadAll]);
  useEffect(() => { setLang(siteLang); }, [siteLang]);

  const forceRefresh = async () => { await refreshIssuesCache(); await loadAll(); };
  const onPendingPage = (p: number) => { setPendingPage(p); loadPending(p); setSelected(new Set()); };
  const onIpPage = (p: number) => { setIpPage(p); loadInProgress(p); };
  const onDonePage = (p: number) => { setDonePage(p); loadDone(p); };
  const onInaccuratePage = (p: number) => { setInaccuratePage(p); loadInaccurate(p); };

  const startAnalysis = async (issueId: string, isRetry = false) => {
    try {
      const task = await createTask(issueId, undefined, isRetry ? "" : (username || ""));
      setActiveTasks((p) => ({ ...p, [issueId]: task }));
      // Remove from pending list (new analysis)
      setPendingData((prev) => {
        if (!prev) return prev;
        const exists = prev.issues.some((i) => i.record_id === issueId);
        if (!exists) return prev;
        return { ...prev, issues: prev.issues.filter((i) => i.record_id !== issueId), total: Math.max(0, prev.total - 1) };
      });
      // Remove from done list (retry of failed item)
      setDoneData((prev) => {
        if (!prev) return prev;
        const exists = prev.issues.some((i) => i.record_id === issueId);
        if (!exists) return prev;
        return { ...prev, issues: prev.issues.filter((i) => i.record_id !== issueId), total: Math.max(0, prev.total - 1) };
      });
      await loadInProgress(1); setTab("in_progress");
      subscribeTaskProgress(task.task_id, (progress) => {
        setActiveTasks((p) => ({ ...p, [issueId]: progress }));
        if (progress.status === "done") {
          fetchTaskResult(task.task_id).then((r) => {
            setActiveResults((p) => ({ ...p, [issueId]: r }));
            loadInProgress(1); loadDone(1);
          }).catch(console.error);
        }
        if (progress.status === "failed") {
          setToast(`${t("分析失败")}: ${progress.error || t("未知错误")}`);
          loadInProgress(1); loadPending(pendingPage);
        }
      });
    } catch (e: any) { setError(e.message); }
  };

  const batchStart = async () => { for (const id of selected) await startAnalysis(id); setSelected(new Set()); };
  const toggle = (id: string) => setSelected((p) => { const n = new Set(p); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const copy = (text: string) => { navigator.clipboard.writeText(text); setToast(t("已复制到剪贴板")); };

  const handleDelete = async (issueId: string) => {
    if (!confirm(t("确定要删除这个工单吗？"))) return;
    try {
      await deleteIssue(issueId);
      setToast(t("工单已删除")); loadInProgress(ipPage); loadDone(donePage);
    } catch (e: any) { setToast(`${t("删除失败")}: ${e.message}`); }
  };

  const handleMarkInaccurate = async (issueId: string) => {
    try {
      await markInaccurate(issueId);
      setToast(t("已标记为不准确"));
      loadDone(donePage);
      loadInaccurate(inaccuratePage);
      if (detailId === issueId) closeDetail();
    } catch (e: any) { setToast(`${t("失败")}: ${e.message}`); }
  };

  const startFollowup = async (issueId: string, question: string) => {
    if (!question.trim()) return;
    setFollowupSubmitting(true);
    try {
      const task = await createTask(issueId, undefined, username || "", question.trim());
      setActiveTasks((p) => ({ ...p, [issueId]: task }));
      subscribeTaskProgress(task.task_id, (progress) => {
        setActiveTasks((p) => ({ ...p, [issueId]: progress }));
        if (progress.status === "done") {
          // Reload all analyses for this issue
          fetchIssueAnalyses(issueId).then((analyses) => {
            setIssueAnalyses((prev) => ({ ...prev, [issueId]: analyses }));
          }).catch(() => {});
          fetchTaskResult(task.task_id).then((r) => {
            setActiveResults((p) => ({ ...p, [issueId]: r }));
            loadDone(1);
          }).catch(console.error);
          setFollowupSubmitting(false);
          setFollowupText("");
        }
        if (progress.status === "failed") {
          setToast(`${t("分析失败")}: ${progress.error || t("未知错误")}`);
          setFollowupSubmitting(false);
        }
      });
    } catch (e: any) {
      setToast(e.message);
      setFollowupSubmitting(false);
    }
  };

  const counts = { pending: pendingData?.total ?? 0, in_progress: ipData?.total ?? 0, done: doneData?.total ?? 0, inaccurate: inaccurateData?.total ?? 0 };

  const openDetail = (id: string, t: Tab) => { setDetailId(id); setDirectDetail(null); setDetailTab(t); setUrlParam("detail", id); setUrlParam("dtab", t); };
  const closeDetail = () => { setDetailId(null); setDirectDetail(null); setFollowupText(""); setFollowupSubmitting(false); setUrlParam("detail", ""); setUrlParam("dtab", ""); };

  const detailData = (() => {
    if (!detailId) return null;
    if (detailTab === "pending") {
      const issue = pendingData?.issues.find((i) => i.record_id === detailId);
      if (issue) return { issue, task: activeTasks[detailId], result: activeResults[detailId], localItem: null as LocalIssueItem | null };
    } else {
      const items = detailTab === "in_progress" ? ipData?.issues : detailTab === "inaccurate" ? inaccurateData?.issues : doneData?.issues;
      const item = items?.find((i) => i.record_id === detailId);
      if (item) return { issue: item as any as Issue, task: item.task as any, result: item.analysis || activeResults[detailId], localItem: item };
    }
    if (directDetail) {
      return { issue: directDetail as any as Issue, task: directDetail.task as any, result: directDetail.analysis || null, localItem: directDetail };
    }
    return null;
  })();

  useEffect(() => {
    if (!detailId || detailData) return;
    let cancelled = false;
    fetchIssueDetail(detailId).then((item) => {
      if (!cancelled) {
        setDirectDetail(item);
        if (item.local_status === "inaccurate") setDetailTab("inaccurate");
        else if (item.local_status === "done" || item.local_status === "failed") setDetailTab("done");
        else if (item.local_status === "analyzing") setDetailTab("in_progress");
      }
    }).catch(() => {
      if (!cancelled) {
        setToast(`${t("加载失败")}: ${detailId}`);
        closeDetail();
      }
    });
    return () => { cancelled = true; };
  }, [detailId, detailData]);

  // Load all analyses for the issue when detail panel opens with a result
  useEffect(() => {
    if (!detailId) return;
    const hasResult = detailData?.result;
    if (!hasResult) return;
    let cancelled = false;
    fetchIssueAnalyses(detailId).then((analyses) => {
      if (!cancelled && analyses.length > 0) {
        setIssueAnalyses((prev) => ({ ...prev, [detailId]: analyses }));
      }
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [detailId, detailData?.result]);

  // ── Render ──────────────────────────────────────────────────
  const thStyle = { color: S.text3, fontSize: "10px", fontWeight: 600, textTransform: "uppercase" as const, letterSpacing: "0.08em", padding: "10px 12px", textAlign: "left" as const };
  const tdBase = "px-3 py-3 align-top";

  const TableHeader = ({ cols }: { cols: React.ReactNode[] }) => (
    <thead>
      <tr style={{ borderBottom: `1px solid ${S.border}`, background: "rgba(0,0,0,0.02)" }}>
        {cols.map((col, i) => <th key={i} style={thStyle}>{col}</th>)}
      </tr>
    </thead>
  );

  return (
    <div className="min-h-full">
      {/* Header */}
      <header className="sticky top-0 z-10 backdrop-blur-md"
        style={{ background: "rgba(255,255,255,0.92)", borderBottom: `1px solid ${S.border}` }}>
        <div className="flex items-center justify-between px-6 py-3">
          <div className="flex items-center gap-4">
            <h1 className="text-base font-semibold" style={{ color: S.text1 }}>{t("工单分析")}</h1>
            {/* Assignee filter */}
            <div className="flex items-center gap-1.5">
              {!showAssigneeEdit ? (
                <button onClick={() => setShowAssigneeEdit(true)}
                  className="flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-xs transition-colors"
                  style={{ border: `1px solid ${S.border}`, color: S.text2 }}>
                  <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 6a3.75 3.75 0 11-7.5 0 3.75 3.75 0 017.5 0zM4.501 20.118a7.5 7.5 0 0114.998 0" />
                  </svg>
                  {assignee
                    ? <span className="font-medium" style={{ color: S.text1 }}>{assignee}</span>
                    : <span>{t("全部指派人")}</span>}
                </button>
              ) : (
                <div className="flex items-center gap-1">
                  <input autoFocus value={assigneeInput} onChange={(e) => setAssigneeInput(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter") applyAssignee(); if (e.key === "Escape") setShowAssigneeEdit(false); }}
                    placeholder="指派人"
                    className="w-28 rounded-lg px-2.5 py-1 text-xs outline-none font-sans"
                    style={{ background: S.overlay, border: `1px solid ${S.border}`, color: S.text1 }} />
                  <button onClick={applyAssignee} className="rounded-lg px-2 py-1 text-[11px] font-semibold"
                    style={{ background: S.accent, color: "#0A0B0E" }}>{t("确定")}</button>
                  {assignee && <button onClick={clearAssignee} className="rounded-lg px-2 py-1 text-[11px]"
                    style={{ border: `1px solid ${S.border}`, color: S.text2 }}>{t("清除")}</button>}
                  <button onClick={() => setShowAssigneeEdit(false)} className="text-[11px]" style={{ color: S.text3 }}>{t("取消")}</button>
                </div>
              )}
            </div>
          </div>
          <div className="flex items-center gap-2">
            {selected.size > 0 && (
              <button onClick={batchStart} className={`${btnPrimary}`}
                style={{ background: S.accent, color: "#0A0B0E" }}>
                {t("批量分析")} ({selected.size})
              </button>
            )}
            <a href="/feedback" className={`${btnGhost}`}
              style={{ border: `1px solid ${S.border}`, color: S.text2 }}>
              {t("提交反馈")}
            </a>
            <button onClick={loadAll} disabled={loading} className={`${btnGhost} disabled:opacity-50`}
              style={{ border: `1px solid ${S.border}`, color: S.text2 }}>
              {loading ? t("加载中...") : t("刷新")}
            </button>
            <button onClick={forceRefresh} disabled={loading} className={`${btnGhost} disabled:opacity-50`}
              style={{ border: `1px solid ${S.border}`, color: S.text2 }}>
              {t("同步飞书")}
            </button>
            {/* Username */}
            <div className="ml-1 pl-3" style={{ borderLeft: `1px solid ${S.border}` }}>
              {!showUsernameEdit ? (
                <button onClick={() => { setShowUsernameEdit(true); setUsernameInput(username || ""); }}
                  className="flex items-center gap-1.5 rounded-lg px-2 py-1 text-xs transition-colors">
                  <span className="flex h-6 w-6 items-center justify-center rounded-full text-[10px] font-bold"
                    style={{ background: S.accent, color: "#0A0B0E" }}>
                    {username ? username[0].toUpperCase() : "?"}
                  </span>
                  <span className="font-medium" style={{ color: S.text2 }}>
                    {username || t("设置用户名")}
                  </span>
                </button>
              ) : (
                <div className="flex items-center gap-1">
                  <input autoFocus value={usernameInput} onChange={(e) => setUsernameInput(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter") saveUsername(usernameInput); if (e.key === "Escape") setShowUsernameEdit(false); }}
                    placeholder="用户名"
                    className="w-24 rounded-lg px-2.5 py-1 text-xs outline-none font-sans"
                    style={{ background: S.overlay, border: `1px solid ${S.border}`, color: S.text1 }} />
                  <button onClick={() => saveUsername(usernameInput)} className="rounded-lg px-2 py-1 text-[11px] font-semibold"
                    style={{ background: S.accent, color: "#0A0B0E" }}>{t("保存")}</button>
                  <button onClick={() => setShowUsernameEdit(false)} className="text-[11px]" style={{ color: S.text3 }}>{t("取消")}</button>
                </div>
              )}
            </div>
          </div>
        </div>
      </header>

      <div className="px-6 py-5">
        {/* Loading state */}
        {loading && !ipData && !doneData && !inaccurateData && (
          <div className="flex flex-col items-center justify-center py-24">
            <div className="mb-4 h-8 w-8 animate-spin rounded-full border-4"
              style={{ borderColor: "rgba(0,0,0,0.08)", borderTopColor: S.accent }} />
            <p className="text-sm font-medium" style={{ color: S.text2 }}>{t("正在加载工单...")}</p>
            <p className="mt-1 text-xs" style={{ color: S.text3 }}>{t("首次加载可能需要几秒钟")}</p>
          </div>
        )}

        {/* Stat cards */}
        <div className="mb-5 grid grid-cols-4 gap-3">
          {loading && !ipData && !doneData && !inaccurateData ? (
            [1, 2, 3, 4].map((i) => (
              <div key={i} className="rounded-xl px-4 py-3" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
                <div className="h-2.5 w-12 rounded animate-pulse" style={{ background: S.hover }} />
                <div className="mt-2.5 h-5 w-8 rounded animate-pulse" style={{ background: S.hover }} />
              </div>
            ))
          ) : (
            [
              { label: t("待处理"), value: counts.pending, color: S.text1 },
              { label: t("进行中"), value: counts.in_progress, color: "#2563EB" },
              { label: t("已完成"), value: counts.done, color: "#16A34A" },
              { label: t("分析不准确"), value: counts.inaccurate, color: "#DC2626" },
            ].map((s) => (
              <div key={s.label} className="rounded-xl px-4 py-3" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
                <p className="text-xs" style={{ color: S.text3 }}>{s.label}</p>
                <p className="mt-1 text-xl font-bold tabular-nums" style={{ color: s.color }}>{s.value}</p>
              </div>
            ))
          )}
        </div>

        {error && (
          <div className="mb-4 rounded-lg px-4 py-3 text-sm"
            style={{ background: "rgba(239,68,68,0.1)", color: "#DC2626", border: "1px solid rgba(239,68,68,0.2)" }}>
            {error}
          </div>
        )}

        {/* Tabs */}
        <div className="mb-4 flex items-center gap-1 rounded-lg p-1 w-fit"
          style={{ background: S.overlay }}>
          {([
            { key: "pending" as Tab, label: t("待处理"), count: counts.pending },
            { key: "in_progress" as Tab, label: t("进行中"), count: counts.in_progress },
            { key: "done" as Tab, label: t("已完成"), count: counts.done },
            { key: "inaccurate" as Tab, label: t("分析不准确"), count: counts.inaccurate },
          ]).map((item) => (
            <button key={item.key} onClick={() => setTab(item.key)}
              className="rounded-md px-3 py-1.5 text-sm font-medium transition-all"
              style={tab === item.key
                ? { background: S.surface, color: S.text1, boxShadow: "0 1px 3px rgba(0,0,0,0.3)" }
                : { color: S.text3 }}>
              {item.label}
              {item.count > 0 && (
                <span className="ml-1.5 text-[10px] tabular-nums" style={{ color: tab === item.key ? S.text2 : S.text3 }}>
                  {item.count}
                </span>
              )}
            </button>
          ))}
        </div>

        {/* ── PENDING TAB ── */}
        {tab === "pending" && (
          <>
            {/* Batch action toolbar */}
            {!!pendingData?.issues.length && (
              <div className="mb-3 flex items-center gap-3 rounded-xl px-4 py-2.5"
                style={{ background: selected.size > 0 ? "rgba(184,146,46,0.08)" : S.surface, border: `1px solid ${selected.size > 0 ? "rgba(184,146,46,0.25)" : S.border}`, transition: "all 0.2s" }}>
                <label className="flex items-center gap-2 cursor-pointer select-none">
                  <IndeterminateCheckbox
                    checked={selected.size > 0 && selected.size === (pendingData?.issues || []).length}
                    indeterminate={selected.size > 0 && selected.size < (pendingData?.issues || []).length}
                    onChange={(e) => setSelected(e.target.checked ? new Set((pendingData?.issues || []).map((i) => i.record_id)) : new Set())}
                  />
                  <span className="text-xs font-medium" style={{ color: S.text2 }}>{t("全选本页")}</span>
                </label>
                {selected.size > 0 && (
                  <>
                    <span className="text-xs" style={{ color: S.text3 }}>
                      {t("已选择")} <span className="font-semibold tabular-nums" style={{ color: S.accent }}>{selected.size}</span> {t("个工单")}
                    </span>
                    <button onClick={batchStart}
                      className="rounded-lg px-3.5 py-1.5 text-xs font-semibold transition-colors"
                      style={{ background: S.accent, color: "#0A0B0E" }}>
                      {t("批量分析")} ({selected.size})
                    </button>
                    <button onClick={() => setSelected(new Set())}
                      className="rounded-lg px-2.5 py-1.5 text-xs font-medium transition-colors"
                      style={{ border: `1px solid ${S.border}`, color: S.text3 }}>
                      {t("取消选择")}
                    </button>
                  </>
                )}
              </div>
            )}
            <div className="overflow-hidden rounded-xl" style={{ border: `1px solid ${S.border}`, background: S.surface }}>
              <table className="min-w-full">
                <TableHeader cols={[
                  <IndeterminateCheckbox key="chk"
                    checked={selected.size > 0 && selected.size === (pendingData?.issues || []).length}
                    indeterminate={selected.size > 0 && selected.size < (pendingData?.issues || []).length}
                    onChange={(e) => setSelected(e.target.checked ? new Set((pendingData?.issues || []).map((i) => i.record_id)) : new Set())}
                  />,
                  t("级别"), t("来源"), t("问题描述"), t("设备 SN"), t("Zendesk"), t("飞书"), t("操作")
                ]} />
                <tbody>
                  {pendingLoading ? (
                    <tr><td colSpan={8} className="px-4 py-16 text-center text-sm" style={{ color: S.text3 }}>
                      <span className="inline-flex items-center gap-2">
                        <span className="inline-block h-4 w-4 animate-spin rounded-full border-2" style={{ borderColor: "rgba(0,0,0,0.08)", borderTopColor: S.accent }} />
                        {t("正在从飞书同步...")}
                      </span>
                    </td></tr>
                  ) : !pendingData?.issues.length ? (
                    <tr><td colSpan={8} className="px-4 py-16 text-center text-sm" style={{ color: S.text3 }}>{t("暂无待处理工单")}</td></tr>
                  ) : pendingData.issues.map((issue, idx) => (
                    <tr key={issue.record_id}
                      className="cursor-pointer transition-colors"
                      style={{ borderBottom: `1px solid ${S.borderSm}`, background: idx % 2 === 0 ? "transparent" : "rgba(0,0,0,0.01)" }}
                      onClick={() => openDetail(issue.record_id, "pending")}
                      onMouseEnter={(e) => (e.currentTarget.style.background = S.hover + "60")}
                      onMouseLeave={(e) => (e.currentTarget.style.background = idx % 2 === 0 ? "transparent" : "rgba(0,0,0,0.01)")}>
                      <td className={tdBase} onClick={(e) => e.stopPropagation()} style={{ width: "40px" }}>
                        <input type="checkbox" className="rounded" style={{ accentColor: S.accent }}
                          checked={selected.has(issue.record_id)} onChange={() => toggle(issue.record_id)} />
                      </td>
                      <td className={tdBase} style={{ width: "56px" }}><PriorityBadge p={issue.priority} /></td>
                      <td className={tdBase} style={{ width: "64px" }}><SourceBadge source={issue.source} linearUrl={issue.linear_issue_url} /></td>
                      <td className="px-3 py-3 max-w-md">
                        <p className="text-sm leading-snug" style={{ color: S.text1, display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>
                          {issue.description}
                        </p>
                      </td>
                      <td className={tdBase} style={{ width: "112px" }}>
                        <span className="font-mono text-xs" style={{ color: S.text3 }}>{issue.device_sn || "—"}</span>
                      </td>
                      <td className={tdBase} style={{ width: "80px" }}>
                        {issue.zendesk_id
                          ? <a href={issue.zendesk} target="_blank" onClick={(e) => e.stopPropagation()}
                              className="text-xs font-medium hover:underline" style={{ color: "#2563EB" }}>{issue.zendesk_id}</a>
                          : <span className="text-xs" style={{ color: S.text3 }}>—</span>}
                      </td>
                      <td className={tdBase} style={{ width: "64px" }}>
                        <a href={issue.feishu_link} target="_blank" onClick={(e) => e.stopPropagation()}
                          className="text-xs hover:underline" style={{ color: "#2563EB" }}>{t("链接")}</a>
                      </td>
                      <td className={`${tdBase} text-right`} style={{ width: "96px" }} onClick={(e) => e.stopPropagation()}>
                        <button onClick={() => startAnalysis(issue.record_id)}
                          className="rounded-lg px-3 py-1 text-xs font-semibold transition-colors"
                          style={{ background: S.accent, color: "#0A0B0E" }}>
                          {t("分析")}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Pagination page={pendingPage} totalPages={pendingData?.total_pages ?? 1} onChange={onPendingPage} />
          </>
        )}

        {/* ── IN_PROGRESS / DONE / INACCURATE TABS ── */}
        {(tab === "in_progress" || tab === "done" || tab === "inaccurate") && (() => {
          const data = tab === "in_progress" ? ipData : tab === "inaccurate" ? inaccurateData : doneData;
          const items = data?.issues || [];
          const currentPage = tab === "in_progress" ? ipPage : tab === "inaccurate" ? inaccuratePage : donePage;
          const onPageChange = tab === "in_progress" ? onIpPage : tab === "inaccurate" ? onInaccuratePage : onDonePage;
          const emptyMsg = tab === "in_progress" ? t("暂无进行中工单") : tab === "inaccurate" ? t("暂无不准确工单") : t("暂无已完成工单");
          return (
            <>
              <div className="overflow-hidden rounded-xl" style={{ border: `1px solid ${S.border}`, background: S.surface }}>
                <table className="min-w-full">
                  <TableHeader cols={[
                    t("级别"), t("来源"), t("问题描述"), t("提交人"), t("创建时间"), "Zendesk", t("状态"), t("操作")
                  ]} />
                  <tbody>
                    {loading && !data ? (
                      <tr><td colSpan={8} className="px-4 py-16 text-center text-sm" style={{ color: S.text3 }}>{t("加载中...")}</td></tr>
                    ) : !items.length ? (
                      <tr><td colSpan={8} className="px-4 py-16 text-center text-sm" style={{ color: S.text3 }}>{emptyMsg}</td></tr>
                    ) : items.map((item, idx) => (
                      <tr key={item.record_id}
                        className="cursor-pointer transition-colors"
                        style={{ borderBottom: `1px solid ${S.borderSm}`, background: idx % 2 === 0 ? "transparent" : "rgba(0,0,0,0.01)" }}
                        onClick={() => openDetail(item.record_id, tab)}
                        onMouseEnter={(e) => (e.currentTarget.style.background = S.hover + "60")}
                        onMouseLeave={(e) => (e.currentTarget.style.background = idx % 2 === 0 ? "transparent" : "rgba(0,0,0,0.01)")}>
                        <td className={tdBase} style={{ width: "56px" }}><PriorityBadge p={item.priority} /></td>
                        <td className={tdBase} style={{ width: "64px" }}><SourceBadge source={item.source} linearUrl={item.linear_issue_url} /></td>
                        <td className="px-3 py-3 max-w-md">
                          <p className="text-sm leading-snug" style={{ color: S.text1, display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>
                            {item.description}
                          </p>
                          {item.source === "linear" && item.linear_issue_url && (
                            <a href={item.linear_issue_url} target="_blank" onClick={(e) => e.stopPropagation()}
                              className="mt-1 inline-flex items-center gap-1 text-[11px] hover:underline" style={{ color: "#C4B5FD" }}>
                              {item.linear_issue_id || "Linear"} ↗
                            </a>
                          )}
                          {(item.root_cause_summary || item.result_summary) && (
                            <div className="mt-2 space-y-1 rounded-md px-2.5 py-2" style={{ background: S.overlay }}>
                              {item.root_cause_summary && (
                                <div className="flex items-start gap-1.5">
                                  <span className="mt-px flex-shrink-0 text-[10px] font-semibold" style={{ color: S.accent }}>{t("原因")}</span>
                                  <p className="text-xs" style={{ color: S.text2, display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>{item.root_cause_summary}</p>
                                </div>
                              )}
                              {item.result_summary && (
                                <div className="flex items-start gap-1.5">
                                  <span className="mt-px flex-shrink-0 text-[10px] font-semibold" style={{ color: "#16A34A" }}>{t("结果")}</span>
                                  <p className="text-xs" style={{ color: S.text2, display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>{item.result_summary}</p>
                                </div>
                              )}
                            </div>
                          )}
                        </td>
                        <td className={tdBase} style={{ width: "80px" }}>
                          <span className="text-xs" style={{ color: S.text2 }}>{item.created_by || "—"}</span>
                        </td>
                        <td className={tdBase} style={{ width: "112px" }}>
                          <span className="text-xs font-mono" style={{ color: S.text3 }}>{formatLocalTime(item.created_at)}</span>
                        </td>
                        <td className={tdBase} style={{ width: "80px" }}>
                          {item.zendesk_id
                            ? <a href={item.zendesk} target="_blank" onClick={(e) => e.stopPropagation()}
                                className="text-xs font-medium hover:underline" style={{ color: "#2563EB" }}>{item.zendesk_id}</a>
                            : <span className="text-xs" style={{ color: S.text3 }}>—</span>}
                        </td>
                        <td className={tdBase} style={{ width: "112px" }}><LocalStatusBadge item={item} /></td>
                        <td className={`${tdBase} text-right`} style={{ width: "160px" }} onClick={(e) => e.stopPropagation()}>
                          <div className="flex items-center justify-end gap-1">
                            {item.local_status === "failed" && (
                              <button onClick={() => startAnalysis(item.record_id, true)}
                                className="rounded-lg px-2.5 py-1 text-[11px] font-semibold"
                                style={{ background: S.accent, color: "#0A0B0E" }}>
                                {t("重试")}
                              </button>
                            )}
                            {item.analysis?.user_reply && (
                              <button onClick={() => copy(item.analysis!.user_reply)}
                                className="rounded-lg px-2.5 py-1 text-[11px] font-medium"
                                style={{ background: "rgba(34,197,94,0.15)", color: "#16A34A", border: "1px solid rgba(34,197,94,0.25)" }}>
                                {t("复制回复")}
                              </button>
                            )}
                            {item.local_status === "done" && (
                              <button onClick={() => handleMarkInaccurate(item.record_id)}
                                className="rounded-lg px-2 py-1 text-[11px] font-medium"
                                style={{ background: "rgba(239,68,68,0.10)", color: "#DC2626", border: "1px solid rgba(239,68,68,0.25)" }}>
                                {t("分析不准确")}
                              </button>
                            )}
                            <button onClick={() => handleDelete(item.record_id)}
                              className="rounded-lg p-1 transition-colors"
                              style={{ color: S.text3, border: `1px solid ${S.border}` }}
                              title={t("删除")}>
                              <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                                <path strokeLinecap="round" strokeLinejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" />
                              </svg>
                            </button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <Pagination page={currentPage} totalPages={data?.total_pages ?? 1} onChange={onPageChange} />
            </>
          );
        })()}
      </div>

      {/* ── Detail panel ── */}
      {detailId && detailData && (
        <div className="fixed inset-0 z-50 flex">
          <div className="flex-1 backdrop-blur-sm" style={{ background: "rgba(0,0,0,0.65)" }} onClick={closeDetail} />
          <div className="w-[520px] flex-shrink-0 overflow-y-auto" style={{ background: "#FFFFFF", borderLeft: `1px solid ${S.border}` }}>
            {/* Panel header */}
            <div className="sticky top-0 z-10 flex items-center justify-between px-5 py-3"
              style={{ background: "#FFFFFF", borderBottom: `1px solid ${S.border}` }}>
              <h2 className="text-sm font-semibold" style={{ color: S.text1 }}>{t("工单详情")}</h2>
              <button onClick={closeDetail} className="rounded-lg p-1.5 transition-colors" style={{ color: S.text3 }}>
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>

            <div className="p-5 space-y-5">
              {/* Badges row */}
              <section>
                <div className="flex items-center gap-2 mb-3 flex-wrap">
                  <PriorityBadge p={detailData.issue.priority} />
                  <SourceBadge source={detailData.issue.source || detailData.localItem?.source} linearUrl={detailData.issue.linear_issue_url || detailData.localItem?.linear_issue_url} />
                  {detailData.localItem && <LocalStatusBadge item={detailData.localItem} />}
                  {detailData.issue.zendesk_id && (
                    <a href={detailData.issue.zendesk} target="_blank"
                      className="text-xs font-medium hover:underline" style={{ color: "#2563EB" }}>
                      {detailData.issue.zendesk_id}
                    </a>
                  )}
                  {detailData.issue.feishu_link
                    ? <a href={detailData.issue.feishu_link} target="_blank" className="text-xs hover:underline" style={{ color: "#2563EB" }}>{t("飞书")}</a>
                    : detailData.issue.source !== "linear"
                      ? <span className="text-xs" style={{ color: S.text3 }}>{t("本地上传")}</span>
                      : null}
                  {(detailData.issue.linear_issue_url || detailData.localItem?.linear_issue_url) && (
                    <a href={detailData.issue.linear_issue_url || detailData.localItem?.linear_issue_url} target="_blank"
                      className="text-xs font-medium hover:underline" style={{ color: "#C4B5FD" }}>
                      {detailData.issue.linear_issue_id || detailData.localItem?.linear_issue_id || "Linear"} ↗
                    </a>
                  )}
                </div>
                {/* Meta grid */}
                <div className="grid grid-cols-2 gap-2 text-xs">
                  {[
                    { l: t("设备 SN"), v: detailData.issue.device_sn, mono: true },
                    { l: t("固件"), v: detailData.issue.firmware },
                    { l: t("APP"), v: detailData.issue.app_version },
                    { l: t("日志"), v: `${detailData.issue.log_files?.length || 0} ${t("个")}` },
                  ].map((f) => (
                    <div key={f.l} className="rounded-lg px-3 py-2" style={{ background: S.overlay }}>
                      <span style={{ color: S.text3 }}>{f.l}</span>
                      <p className={`mt-0.5 font-medium ${f.mono ? "font-mono" : ""}`} style={{ color: S.text1 }}>
                        {f.v || "—"}
                      </p>
                    </div>
                  ))}
                </div>
              </section>

              {/* Description */}
              <section>
                <h3 className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>{t("问题描述")}</h3>
                <div className="whitespace-pre-wrap rounded-lg p-3 text-sm leading-relaxed" style={{ background: S.overlay, color: S.text2 }}>
                  {detailData.issue.description || t("无")}
                </div>
              </section>

              {/* Attachments */}
              {detailData.issue.log_files && detailData.issue.log_files.length > 0 && (() => {
                const issueId = detailData.issue.record_id;
                const images = detailData.issue.log_files.filter((f: any) => /\.(png|jpe?g|gif|webp|bmp)$/i.test(f.name));
                const logs = detailData.issue.log_files.filter((f: any) => !/\.(png|jpe?g|gif|webp|bmp)$/i.test(f.name));
                return (
                  <section>
                    <h3 className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>
                      {t("附件")} ({detailData.issue.log_files.length})
                    </h3>
                    {images.length > 0 && (
                      <div className="mb-2 grid grid-cols-3 gap-2">
                        {images.map((f: any, i: number) => (
                          <a key={i} href={`/api/local/${issueId}/files/${f.name}`} target="_blank"
                            className="block overflow-hidden rounded-lg" style={{ border: `1px solid ${S.border}` }}>
                            <img src={`/api/local/${issueId}/files/${f.name}`} alt={f.name} className="h-24 w-full object-cover" loading="lazy" />
                          </a>
                        ))}
                      </div>
                    )}
                    {logs.length > 0 && (
                      <div className="space-y-1">
                        {logs.map((f: any, i: number) => (
                          <div key={i} className="flex items-center gap-2 rounded-md px-2.5 py-1.5 text-xs"
                            style={{ background: S.overlay, color: S.text2 }}>
                            <svg className="h-3.5 w-3.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5} style={{ color: S.text3 }}>
                              <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" />
                            </svg>
                            <span className="truncate">{f.name}</span>
                            {f.size > 0 && <span className="flex-shrink-0" style={{ color: S.text3 }}>{(f.size / 1024 / 1024).toFixed(1)}MB</span>}
                          </div>
                        ))}
                      </div>
                    )}
                  </section>
                );
              })()}

              {/* Start analysis */}
              {detailTab === "pending" && !detailData.task && !detailData.result && (
                <button onClick={() => { startAnalysis(detailId!); closeDetail(); }}
                  className="w-full rounded-lg py-2.5 text-sm font-semibold transition-colors"
                  style={{ background: S.accent, color: "#0A0B0E" }}>
                  {t("开始 AI 分析")}
                </button>
              )}

              {/* Progress */}
              {detailData.task && typeof detailData.task === "object" && "status" in detailData.task
                && !["done", "failed"].includes(detailData.task.status) && (
                <div className="rounded-lg p-4" style={{ background: S.overlay, border: `1px solid ${S.border}` }}>
                  <div className="mb-2 flex justify-between text-xs" style={{ color: S.text2 }}>
                    <span>{detailData.task.message}</span>
                    <span className="tabular-nums">{detailData.task.progress}%</span>
                  </div>
                  <div className="h-1.5 rounded-full overflow-hidden" style={{ background: S.hover }}>
                    <div className="h-full rounded-full transition-all duration-700"
                      style={{ width: `${detailData.task.progress}%`, background: S.accent }} />
                  </div>
                </div>
              )}

              {/* Failed */}
              {detailData.task && typeof detailData.task === "object" && detailData.task.status === "failed" && (
                <>
                  <div className="rounded-lg p-3" style={{ background: "rgba(239,68,68,0.08)", border: "1px solid rgba(239,68,68,0.2)" }}>
                    <p className="text-sm font-medium" style={{ color: "#DC2626" }}>{t("分析失败")}</p>
                    <p className="mt-1 text-xs" style={{ color: "#FCA5A5" }}>{detailData.task.error}</p>
                  </div>
                  <button onClick={() => { startAnalysis(detailId!, true); closeDetail(); }}
                    className="w-full rounded-lg py-2.5 text-sm font-semibold"
                    style={{ background: S.accent, color: "#0A0B0E" }}>
                    {t("重新分析")}
                  </button>
                </>
              )}

              {/* Result — stacked analyses (newest first) */}
              {detailData.result && (() => {
                // Use all analyses if loaded, otherwise fall back to single result
                const allAnalyses = issueAnalyses[detailId!];
                const analyses = allAnalyses && allAnalyses.length > 0 ? allAnalyses : [detailData.result];
                return (
                  <>
                    {/* Language toggle — shared across all analyses */}
                    <section>
                      <div className="mb-2 flex items-center justify-between">
                        <h3 className="text-[10px] font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>
                          {lang === "cn" ? "AI 分析结果" : "AI Analysis"}
                          {analyses.length > 1 && <span className="ml-1.5 text-[10px] font-normal" style={{ color: S.text3 }}>({analyses.length})</span>}
                        </h3>
                        <div className="flex items-center gap-0.5 rounded-md p-0.5" style={{ background: S.overlay }}>
                          <button onClick={() => setLang("cn")}
                            className="rounded px-2 py-0.5 text-[11px] font-medium transition-all"
                            style={lang === "cn" ? { background: S.surface, color: S.text1 } : { color: S.text3 }}>
                            中文
                          </button>
                          <button onClick={() => setLang("en")}
                            className="rounded px-2 py-0.5 text-[11px] font-medium transition-all"
                            style={lang === "en" ? { background: S.surface, color: S.text1 } : { color: S.text3 }}>
                            EN
                          </button>
                        </div>
                      </div>
                    </section>

                    {/* Follow-up input */}
                    <section className="rounded-lg p-3" style={{ background: S.overlay, border: `1px solid ${S.border}` }}>
                      <textarea
                        value={followupText}
                        onChange={(e) => setFollowupText(e.target.value)}
                        placeholder={t("请输入追问内容...")}
                        rows={2}
                        disabled={followupSubmitting}
                        className="w-full resize-none rounded-md px-3 py-2 text-sm outline-none"
                        style={{ background: S.surface, border: `1px solid ${S.borderSm}`, color: S.text1 }}
                      />
                      <div className="mt-2 flex items-center justify-between">
                        <span className="text-[10px]" style={{ color: S.text3 }}>
                          {followupSubmitting ? t("追问分析中...") : t("追问")}
                        </span>
                        <button
                          onClick={() => startFollowup(detailId!, followupText)}
                          disabled={!followupText.trim() || followupSubmitting}
                          className="rounded-lg px-3 py-1 text-[11px] font-semibold transition-colors disabled:opacity-30"
                          style={{ background: S.accent, color: "#0A0B0E" }}>
                          {followupSubmitting ? t("追问分析中...") : t("提交追问")}
                        </button>
                      </div>
                    </section>

                    {/* Follow-up progress */}
                    {followupSubmitting && activeTasks[detailId!] && !["done", "failed"].includes(activeTasks[detailId!].status) && (
                      <div className="rounded-lg p-3" style={{ background: S.overlay, border: `1px solid ${S.border}` }}>
                        <div className="mb-2 flex justify-between text-xs" style={{ color: S.text2 }}>
                          <span>{activeTasks[detailId!].message}</span>
                          <span className="tabular-nums">{activeTasks[detailId!].progress}%</span>
                        </div>
                        <div className="h-1.5 rounded-full overflow-hidden" style={{ background: S.hover }}>
                          <div className="h-full rounded-full transition-all duration-700"
                            style={{ width: `${activeTasks[detailId!].progress}%`, background: S.accent }} />
                        </div>
                      </div>
                    )}

                    {/* Stacked analyses */}
                    {analyses.map((r, idx) => {
                      const isLatest = idx === 0;
                      const problemType = lang === "en" ? (r.problem_type_en || r.problem_type) : r.problem_type;
                      const rootCause = lang === "en" ? (r.root_cause_en || r.root_cause) : r.root_cause;
                      const userReply = lang === "en" ? (r.user_reply_en || r.user_reply) : r.user_reply;
                      const hasEnTranslation = !!(r.problem_type_en && r.root_cause_en);
                      const isFollowup = !!(r as any).followup_question;
                      return (
                        <div key={r.task_id || idx}
                          className="space-y-4 rounded-lg p-4"
                          style={{
                            background: S.surface,
                            border: `1px solid ${S.border}`,
                            borderLeft: isLatest ? `3px solid ${S.accent}` : `3px solid ${S.border}`,
                            opacity: isLatest ? 1 : 0.75,
                          }}>
                          {/* Follow-up question badge or Initial analysis label */}
                          <div className="flex items-center gap-2 flex-wrap">
                            {isFollowup ? (
                              <span className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold"
                                style={{ background: "rgba(167,139,250,0.12)", color: "#C4B5FD", border: "1px solid rgba(167,139,250,0.25)" }}>
                                {t("追问分析")}
                              </span>
                            ) : analyses.length > 1 ? (
                              <span className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium"
                                style={{ background: "rgba(0,0,0,0.04)", color: S.text3, border: `1px solid ${S.borderSm}` }}>
                                {t("初次分析")}
                              </span>
                            ) : null}
                            {(r as any).created_at && (
                              <span className="text-[10px]" style={{ color: S.text3 }}>{formatLocalTime((r as any).created_at)}</span>
                            )}
                          </div>

                          {/* Follow-up question */}
                          {isFollowup && (
                            <div className="rounded-md px-3 py-2 text-xs"
                              style={{ background: "rgba(167,139,250,0.06)", border: "1px solid rgba(167,139,250,0.15)", color: "#C4B5FD" }}>
                              <span className="font-semibold">{t("追问问题")}:</span> {(r as any).followup_question}
                            </div>
                          )}

                          {lang === "en" && !hasEnTranslation && (
                            <p className="text-[10px]" style={{ color: S.accent }}>English translation not available. Showing Chinese.</p>
                          )}
                          <div className="flex flex-wrap gap-2">
                            <span className="rounded-lg px-2.5 py-1 text-xs font-semibold" style={{ background: S.overlay, color: S.text1 }}>
                              {problemType}
                            </span>
                            <ConfBadge c={r.confidence} />
                            {r.needs_engineer && (
                              <span className="rounded-lg px-2.5 py-1 text-xs font-semibold"
                                style={{ background: S.accentBg, color: S.accent, border: `1px solid rgba(184,146,46,0.25)` }}>
                                {lang === "cn" ? "需工程师" : "Engineer needed"}
                              </span>
                            )}
                          </div>
                          {/* Lost recording tool hint */}
                          {r.problem_type && /录音.{0,8}找不到|找不到.{0,8}录音|recording.*lost|lost.*recording|missing.*recording/i.test(r.problem_type + " " + (r.problem_type_en || "")) && (
                            <a href="/tools"
                              className="flex items-center gap-2 rounded-lg px-3 py-2 text-xs font-medium transition-colors"
                              style={{ background: "rgba(96,165,250,0.08)", color: "#2563EB", border: "1px solid rgba(96,165,250,0.2)", textDecoration: "none" }}>
                              <svg className="h-3.5 w-3.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                                <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                              </svg>
                              {lang === "cn" ? "录音找不到？试试录音丢失排查工具 →" : "Can't find the recording? Try the Lost Recording Finder →"}
                            </a>
                          )}
                          <div>
                            <h3 className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>
                              {lang === "cn" ? "问题原因" : "Root Cause"}
                            </h3>
                            <div className="whitespace-pre-wrap rounded-lg p-3 text-sm" style={{ background: S.overlay, color: S.text2 }}>
                              {rootCause}
                            </div>
                          </div>
                          {r.key_evidence && r.key_evidence.length > 0 && (
                            <div>
                              <h3 className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>
                                {lang === "cn" ? "关键证据" : "Key Evidence"}
                              </h3>
                              <div className="space-y-1">
                                {r.key_evidence.map((ev, i) => (
                                  <div key={i} className="rounded font-mono px-3 py-1.5 text-[11px]"
                                    style={{ background: S.overlay, color: S.text2, border: `1px solid ${S.borderSm}` }}>
                                    {ev}
                                  </div>
                                ))}
                              </div>
                            </div>
                          )}
                          {userReply && (
                            <div>
                              <div className="mb-1.5 flex items-center justify-between">
                                <h3 className="text-[10px] font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>
                                  {lang === "cn" ? "建议回复" : "Suggested Reply"}
                                </h3>
                                <button onClick={() => copy(userReply)}
                                  className="rounded-lg px-3 py-1 text-[11px] font-medium"
                                  style={{ background: "rgba(34,197,94,0.15)", color: "#16A34A", border: "1px solid rgba(34,197,94,0.25)" }}>
                                  {lang === "cn" ? "一键复制" : "Copy"}
                                </button>
                              </div>
                              <div className="whitespace-pre-wrap rounded-lg p-3 text-sm"
                                style={{ background: S.overlay, color: S.text2, borderLeft: "2px solid rgba(34,197,94,0.4)" }}>
                                {userReply}
                              </div>
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </>
                );
              })()}

              {/* Retry for failed */}
              {detailData.localItem?.local_status === "failed" && (
                <section>
                  <button onClick={() => { startAnalysis(detailId!, true); closeDetail(); }}
                    className="w-full rounded-lg py-2.5 text-sm font-semibold"
                    style={{ background: S.accent, color: "#0A0B0E" }}>
                    {t("重新分析")}
                  </button>
                </section>
              )}

              {/* Transfer to Feishu + Mark inaccurate */}
              <section className="pt-4 space-y-2" style={{ borderTop: `1px solid ${S.border}` }}>
                <button onClick={() => {
                    const base = "https://nicebuild.feishu.cn/share/base/form/shrcnGuYEnRrbbVw4Y6evkyUDCo";
                    const params = new URLSearchParams();
                    const issue = detailData!.issue;
                    const appUrl = `${window.location.origin}/tracking?issue=${detailId}`;
                    const desc = `Appllo 工单: ${appUrl}\n\n${issue.description || ""}`;
                    params.set("prefill_问题描述", desc);
                    if (issue.zendesk) params.set("prefill_Zendesk 工单链接", issue.zendesk);
                    if (issue.feishu_link) params.set("prefill_飞书工单链接", issue.feishu_link);
                    const latestAnalysis = issueAnalyses[detailId!]?.[0] || detailData!.result;
                    if (latestAnalysis?.root_cause) params.set("prefill_处理结果", latestAnalysis.root_cause);
                    if (issue.root_cause_summary) params.set("prefill_一句话归因", issue.root_cause_summary);
                    window.open(`${base}?${params.toString()}`, "_blank");
                  }}
                  className="w-full rounded-lg py-2.5 text-sm font-semibold flex items-center justify-center gap-2"
                  style={{ background: "rgba(96,165,250,0.12)", color: "#2563EB", border: "1px solid rgba(96,165,250,0.25)" }}>
                  <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 6H5.25A2.25 2.25 0 003 8.25v10.5A2.25 2.25 0 005.25 21h10.5A2.25 2.25 0 0018 18.75V10.5m-10.5 6L21 3m0 0h-5.25M21 3v5.25" />
                  </svg>
                  {t("转飞书工单")}
                </button>
                {detailData.localItem?.local_status === "done" && (
                  <button
                    onClick={() => handleMarkInaccurate(detailId!)}
                    className="w-full rounded-lg py-2.5 text-sm font-medium transition-colors"
                    style={{ background: "rgba(239,68,68,0.10)", color: "#DC2626", border: "1px solid rgba(239,68,68,0.25)" }}>
                    {t("标记为不准确")}
                  </button>
                )}
              </section>
            </div>
          </div>
        </div>
      )}

      {/* ── First-time username setup ── */}
      {showUsernameSetup && (
        <div className="fixed inset-0 z-[70] flex items-center justify-center" style={{ background: "rgba(0,0,0,0.75)" }}>
          <div className="w-full max-w-sm rounded-2xl p-6" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
            <div className="mb-5 text-center">
              <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-full"
                style={{ background: S.accent }}>
                <svg width="22" height="22" viewBox="0 0 24 24" fill="none">
                  <circle cx="10.5" cy="10.5" r="6" stroke="#0A0B0E" strokeWidth="2.5" />
                  <path d="M15 15L20.5 20.5" stroke="#0A0B0E" strokeWidth="2.5" strokeLinecap="round" />
                  <path d="M10.5 7V8.5M10.5 12.5V14M8 10.5H6.5M14.5 10.5H13" stroke="#0A0B0E" strokeWidth="1.5" strokeLinecap="round" />
                  <circle cx="10.5" cy="10.5" r="1.2" fill="#0A0B0E" />
                </svg>
              </div>
              <h3 className="text-base font-semibold" style={{ color: S.text1 }}>{t("欢迎使用 Appllo")}</h3>
              <p className="mt-1 text-sm" style={{ color: S.text2 }}>{t("请设置您的用户名，用于标记工单操作")}</p>
            </div>
            <input
              autoFocus
              value={usernameInput}
              onChange={(e) => setUsernameInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && usernameInput.trim()) saveUsername(usernameInput); }}
              placeholder={t("输入您的名字")}
              className="mb-4 w-full rounded-lg px-4 py-2.5 text-center text-sm outline-none font-sans"
              style={{ background: S.overlay, border: `1px solid ${S.border}`, color: S.text1 }}
            />
            <button
              onClick={() => saveUsername(usernameInput)}
              disabled={!usernameInput.trim()}
              className="w-full rounded-lg py-2.5 text-sm font-semibold transition-colors disabled:opacity-30"
              style={{ background: S.accent, color: "#0A0B0E" }}>
              {t("开始使用")}
            </button>
          </div>
        </div>
      )}

      {toast && <Toast msg={toast} onClose={() => setToast("")} />}
    </div>
  );
}
