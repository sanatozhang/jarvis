"use client";

import { useEffect, useState, useCallback } from "react";
import { useT } from "@/lib/i18n";
import {
  fetchPendingIssues,
  refreshIssuesCache,
  fetchCompleted,
  fetchInProgress,
  createTask,
  deleteIssue,
  escalateIssue,
  loginUser,
  subscribeTaskProgress,
  fetchTaskResult,
  type Issue,
  type TaskProgress,
  type AnalysisResult,
  type LocalIssueItem,
  type PaginatedResponse,
} from "@/lib/api";

// ============================================================
// URL helpers
// ============================================================
function getUrlParam(key: string): string {
  if (typeof window === "undefined") return "";
  return new URLSearchParams(window.location.search).get(key) || "";
}
function setUrlParam(key: string, val: string) {
  const url = new URL(window.location.href);
  val ? url.searchParams.set(key, val) : url.searchParams.delete(key);
  window.history.replaceState({}, "", url.toString());
}

// ============================================================
// Small components
// ============================================================
function PriorityBadge({ p }: { p: string }) {
  return p === "H"
    ? <span className="inline-flex items-center rounded-full bg-red-50 px-2 py-0.5 text-[11px] font-semibold text-red-600 ring-1 ring-red-200">高</span>
    : <span className="inline-flex items-center rounded-full bg-gray-50 px-2 py-0.5 text-[11px] font-medium text-gray-500 ring-1 ring-gray-200">低</span>;
}

function ConfBadge({ c }: { c: string }) {
  const m: Record<string, string> = { high: "text-green-700 bg-green-50 ring-green-200", medium: "text-yellow-700 bg-yellow-50 ring-yellow-200", low: "text-red-700 bg-red-50 ring-red-200" };
  return <span className={`inline-flex rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ${m[c] || m.low}`}>{c}</span>;
}

function LocalStatusBadge({ item }: { item: LocalIssueItem }) {
  const task = item.task;
  const analysis = item.analysis;
  if (task && !["done", "failed"].includes(task.status)) {
    const labels: Record<string, string> = { queued: "排队中", downloading: "下载中", decrypting: "解密中", extracting: "提取中", analyzing: t("分析中") };
    return <span className="inline-flex items-center gap-1 rounded-full bg-blue-50 px-2 py-0.5 text-[11px] font-medium text-blue-600"><span className="h-1.5 w-1.5 animate-pulse rounded-full bg-blue-500" />{labels[task.status] || task.status}</span>;
  }
  if ((item.local_status === "done" || analysis) && analysis) {
    // Rule matched (not "general") → show 100% confidence badge
    const ruleMatched = analysis.rule_type && analysis.rule_type !== "general";
    return (
      <span className="inline-flex items-center gap-1">
        <span className="inline-flex items-center gap-1 rounded-full bg-green-50 px-2 py-0.5 text-[11px] font-medium text-green-700 ring-1 ring-green-200">
          <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}><path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" /></svg>
          成功
        </span>
        {ruleMatched && (
          <span className="inline-flex items-center rounded-full bg-emerald-100 px-1.5 py-0.5 text-[10px] font-bold text-emerald-700" title={`命中规则: ${analysis.rule_type}`}>
            100%
          </span>
        )}
      </span>
    );
  }
  if (item.local_status === "done")
    return <span className="inline-flex items-center gap-1 rounded-full bg-green-50 px-2 py-0.5 text-[11px] font-medium text-green-700 ring-1 ring-green-200"><svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}><path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" /></svg>成功</span>;
  if (item.local_status === "failed")
    return <span className="inline-flex items-center gap-1 rounded-full bg-red-50 px-2 py-0.5 text-[11px] font-medium text-red-600 ring-1 ring-red-200">分析失败</span>;
  return <span className="text-xs text-gray-300">—</span>;
}

function Toast({ msg, onClose }: { msg: string; onClose: () => void }) {
  useEffect(() => { const t = setTimeout(onClose, 2500); return () => clearTimeout(t); }, [onClose]);
  return <div className="fixed bottom-6 right-6 z-50 rounded-lg bg-gray-900 px-4 py-2.5 text-sm font-medium text-white shadow-lg">{msg}</div>;
}

function Pagination({ page, totalPages, onChange }: { page: number; totalPages: number; onChange: (p: number) => void }) {
  if (totalPages <= 1) return null;
  return (
    <div className="mt-4 flex items-center justify-center gap-2">
      <button disabled={page <= 1} onClick={() => onChange(page - 1)} className="rounded-md border border-gray-200 px-3 py-1.5 text-xs font-medium text-gray-600 hover:bg-gray-50 disabled:opacity-30 disabled:cursor-not-allowed">上一页</button>
      <span className="text-xs tabular-nums text-gray-400">{page} / {totalPages}</span>
      <button disabled={page >= totalPages} onClick={() => onChange(page + 1)} className="rounded-md border border-gray-200 px-3 py-1.5 text-xs font-medium text-gray-600 hover:bg-gray-50 disabled:opacity-30 disabled:cursor-not-allowed">下一页</button>
    </div>
  );
}

// ============================================================
type Tab = "pending" | "in_progress" | "done";
const PAGE_SIZE = 20;

export default function HomePage() {
  const t = useT();
  // --- Per-tab data + pagination ---
  const [pendingData, setPendingData] = useState<PaginatedResponse<Issue> | null>(null);
  const [ipData, setIpData] = useState<PaginatedResponse<LocalIssueItem> | null>(null);
  const [doneData, setDoneData] = useState<PaginatedResponse<LocalIssueItem> | null>(null);

  const [pendingPage, setPendingPage] = useState(1);
  const [ipPage, setIpPage] = useState(1);
  const [donePage, setDonePage] = useState(1);

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // Active analysis tasks (this session)
  const [activeTasks, setActiveTasks] = useState<Record<string, TaskProgress>>({});
  const [activeResults, setActiveResults] = useState<Record<string, AnalysisResult>>({});

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [detailId, setDetailId] = useState<string | null>(null);
  const [lang, setLang] = useState<"cn" | "en">("cn");
  const [detailTab, setDetailTab] = useState<Tab>("pending");
  const [toast, setToast] = useState("");
  const [tab, setTab] = useState<Tab>("pending");

  // Read tab from URL after mount (avoid hydration mismatch)
  useEffect(() => {
    const urlTab = new URLSearchParams(window.location.search).get("tab");
    if (urlTab === "in_progress" || urlTab === "done") setTab(urlTab);
  }, []);

  // --- Username (persisted in localStorage) ---
  const [username, setUsername] = useState<string | null>(null);
  const [usernameInput, setUsernameInput] = useState("");
  const [showUsernameEdit, setShowUsernameEdit] = useState(false);
  const [showUsernameSetup, setShowUsernameSetup] = useState(false); // first-time dialog

  // --- Assignee (read from URL on mount, BEFORE any data fetching) ---
  const [assignee, setAssignee] = useState<string | null>(null);
  const [assigneeInput, setAssigneeInput] = useState("");
  const [showAssigneeEdit, setShowAssigneeEdit] = useState(false);

  useEffect(() => {
    // Read username from localStorage
    const savedName = typeof window !== "undefined" ? localStorage.getItem("jarvis_username") || "" : "";
    if (savedName) {
      setUsername(savedName);
      setUsernameInput(savedName);
    } else {
      setUsername("");
      setShowUsernameSetup(true); // first time — prompt to set username
    }

    // Read assignee
    const fromUrl = getUrlParam("assignee");
    const fromStorage = typeof window !== "undefined" ? localStorage.getItem("jarvis_assignee") || "" : "";
    const a = fromUrl || fromStorage;
    setAssignee(a);
    setAssigneeInput(a);
    if (a) { setUrlParam("assignee", a); localStorage.setItem("jarvis_assignee", a); }
  }, []);

  const saveUsername = async (name: string) => {
    const v = name.trim();
    if (!v) return;
    setUsername(v);
    setUsernameInput(v);
    localStorage.setItem("jarvis_username", v);
    setShowUsernameSetup(false);
    setShowUsernameEdit(false);
    // Register/login user on backend (gets role)
    try {
      const user = await loginUser(v);
      localStorage.setItem("jarvis_role", user.role);
    } catch {} // non-fatal
  };

  const applyAssignee = () => {
    const v = assigneeInput.trim();
    setAssignee(v);
    setUrlParam("assignee", v);
    if (v) { localStorage.setItem("jarvis_assignee", v); } else { localStorage.removeItem("jarvis_assignee"); }
    setShowAssigneeEdit(false);
    setPendingPage(1);
  };
  const clearAssignee = () => {
    setAssignee("");
    setAssigneeInput("");
    setUrlParam("assignee", "");
    localStorage.removeItem("jarvis_assignee");
    setShowAssigneeEdit(false);
    setPendingPage(1);
  };

  // --- Loaders ---
  const loadPending = useCallback(async (page: number) => {
    if (assignee === null) return; // not initialized yet
    try {
      const d = await fetchPendingIssues(assignee || undefined, page, PAGE_SIZE);
      setPendingData(d);
    } catch (e: any) { setError(e.message); }
  }, [assignee]);

  const loadInProgress = useCallback(async (page: number) => {
    try { setIpData(await fetchInProgress(page, PAGE_SIZE)); } catch (e: any) { setError(e.message); }
  }, []);

  const loadDone = useCallback(async (page: number) => {
    try { setDoneData(await fetchCompleted(page, PAGE_SIZE)); } catch (e: any) { setError(e.message); }
  }, []);

  const loadAll = useCallback(async () => {
    if (assignee === null) return; // wait for URL param init
    setLoading(true);
    setError("");
    await Promise.all([loadPending(pendingPage), loadInProgress(ipPage), loadDone(donePage)]);
    setLoading(false);
  }, [assignee, loadPending, loadInProgress, loadDone, pendingPage, ipPage, donePage]);

  useEffect(() => { loadAll(); }, [loadAll]);

  const forceRefresh = async () => { await refreshIssuesCache(); await loadAll(); };

  // Pagination change handlers
  const onPendingPage = (p: number) => { setPendingPage(p); loadPending(p); };
  const onIpPage = (p: number) => { setIpPage(p); loadInProgress(p); };
  const onDonePage = (p: number) => { setDonePage(p); loadDone(p); };

  // --- Analysis ---
  const startAnalysis = async (issueId: string) => {
    try {
      const task = await createTask(issueId, undefined, username || "");
      setActiveTasks((p) => ({ ...p, [issueId]: task }));

      // Remove from pending list (optimistic) + reload in-progress
      // Remove from pending only if the issue is actually in the pending list
      setPendingData((prev) => {
        if (!prev) return prev;
        const exists = prev.issues.some((i) => i.record_id === issueId);
        if (!exists) return prev;
        return { ...prev, issues: prev.issues.filter((i) => i.record_id !== issueId), total: Math.max(0, prev.total - 1) };
      });

      // Backend already marked the issue as "analyzing" synchronously,
      // so in-progress tab will have it. Reload immediately.
      loadInProgress(1);
      setTab("in_progress");

      subscribeTaskProgress(task.task_id, (progress) => {
        setActiveTasks((p) => ({ ...p, [issueId]: progress }));
        if (progress.status === "done") {
          fetchTaskResult(task.task_id).then((r) => {
            setActiveResults((p) => ({ ...p, [issueId]: r }));
            loadInProgress(1);
            loadDone(1);
          }).catch(console.error);
        }
        if (progress.status === "failed") {
          setToast(`分析失败: ${progress.error || "未知错误"}`);
          loadInProgress(1);
          // Reload pending so the issue reappears for retry
          loadPending(pendingPage);
        }
      });
    } catch (e: any) { setError(e.message); }
  };

  const batchStart = async () => { for (const id of selected) await startAnalysis(id); setSelected(new Set()); };
  const toggle = (id: string) => setSelected((p) => { const n = new Set(p); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const copy = (text: string) => { navigator.clipboard.writeText(text); setToast(t("已复制到剪贴板")); };

  const handleDelete = async (issueId: string) => {
    if (!confirm("确定要删除这个工单吗？")) return;
    try {
      await deleteIssue(issueId);
      setToast("工单已删除");
      loadInProgress(ipPage);
      loadDone(donePage);
    } catch (e: any) { setToast(`删除失败: ${e.message}`); }
  };

  const handleEscalate = async (issueId: string) => {
    try {
      const res: any = await escalateIssue(issueId);
      setToast(res.message || (res.status === "sent" ? "已通知值班工程师" : "发送失败"));
    } catch (e: any) { setToast(`通知失败: ${e.message}`); }
  };
  // --- Counts ---
  const counts = {
    pending: pendingData?.total ?? 0,
    in_progress: ipData?.total ?? 0,
    done: doneData?.total ?? 0,
  };

  // --- Detail ---
  const openDetail = (id: string, t: Tab) => { setDetailId(id); setDetailTab(t); };
  const detailData = (() => {
    if (!detailId) return null;
    if (detailTab === "pending") {
      const issue = pendingData?.issues.find((i) => i.record_id === detailId);
      return issue ? { issue, task: activeTasks[detailId], result: activeResults[detailId], localItem: null as LocalIssueItem | null } : null;
    }
    const items = detailTab === "in_progress" ? ipData?.issues : doneData?.issues;
    const item = items?.find((i) => i.record_id === detailId);
    if (!item) return null;
    return { issue: item as any as Issue, task: item.task as any, result: item.analysis || activeResults[detailId], localItem: item };
  })();

  // ============================================================
  return (
    <div className="min-h-full">
      {/* Header */}
      <header className="sticky top-0 z-10 border-b border-gray-200 bg-white/80 backdrop-blur-sm">
        <div className="flex items-center justify-between px-6 py-3">
          <div className="flex items-center gap-4">
            <h1 className="text-lg font-semibold">{t("工单分析")}</h1>
            <div className="flex items-center gap-1.5">
              {!showAssigneeEdit ? (
                <button onClick={() => setShowAssigneeEdit(true)} className="flex items-center gap-1 rounded-lg border border-gray-200 px-2.5 py-1 text-xs text-gray-500 hover:bg-gray-50">
                  <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}><path strokeLinecap="round" strokeLinejoin="round" d="M15.75 6a3.75 3.75 0 11-7.5 0 3.75 3.75 0 017.5 0zM4.501 20.118a7.5 7.5 0 0114.998 0" /></svg>
                  {assignee ? <span className="font-medium text-gray-800">{assignee}</span> : <span>{t("全部指派人")}</span>}

                </button>
              ) : (
                <div className="flex items-center gap-1">
                  <input autoFocus value={assigneeInput} onChange={(e) => setAssigneeInput(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") applyAssignee(); if (e.key === "Escape") setShowAssigneeEdit(false); }} placeholder="指派人" className="w-28 rounded-md border border-gray-300 px-2 py-1 text-xs outline-none focus:border-black" />
                  <button onClick={applyAssignee} className="rounded-md bg-black px-2 py-1 text-[11px] font-medium text-white">{t("确定")}</button>
                  {assignee && <button onClick={clearAssignee} className="rounded-md border border-gray-200 px-2 py-1 text-[11px] text-gray-500">{t("清除")}</button>}
                  <button onClick={() => setShowAssigneeEdit(false)} className="text-[11px] text-gray-400">{t("取消")}</button>
                </div>
              )}
            </div>
          </div>
          <div className="flex items-center gap-2">
            {selected.size > 0 && <button onClick={batchStart} className="rounded-lg bg-black px-4 py-1.5 text-sm font-medium text-white hover:bg-gray-800">批量分析 ({selected.size})</button>}
            <a href="/feedback" className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm font-medium text-gray-600 hover:bg-gray-50">{t("提交反馈")}</a>
            <button onClick={loadAll} disabled={loading} className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm font-medium text-gray-600 hover:bg-gray-50 disabled:opacity-50">{loading ? t(t("加载中...")) : t("刷新")}</button>
            <button onClick={forceRefresh} disabled={loading} className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm font-medium text-gray-500 hover:bg-gray-50 disabled:opacity-50">{t("同步飞书")}</button>
            {/* Username display */}
            <div className="ml-2 border-l border-gray-200 pl-3">
              {!showUsernameEdit ? (
                <button onClick={() => { setShowUsernameEdit(true); setUsernameInput(username || ""); }} className="flex items-center gap-1.5 rounded-lg px-2 py-1 text-xs text-gray-500 hover:bg-gray-50">
                  <span className="flex h-6 w-6 items-center justify-center rounded-full bg-black text-[10px] font-bold text-white">
                    {username ? username[0].toUpperCase() : "?"}
                  </span>
                  <span className="font-medium text-gray-700">{username || "设置用户名"}</span>
                </button>
              ) : (
                <div className="flex items-center gap-1">
                  <input autoFocus value={usernameInput} onChange={(e) => setUsernameInput(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") saveUsername(usernameInput); if (e.key === "Escape") setShowUsernameEdit(false); }} placeholder="用户名" className="w-24 rounded-md border border-gray-300 px-2 py-1 text-xs outline-none focus:border-black" />
                  <button onClick={() => saveUsername(usernameInput)} className="rounded-md bg-black px-2 py-1 text-[11px] font-medium text-white">{t("保存")}</button>
                  <button onClick={() => setShowUsernameEdit(false)} className="text-[11px] text-gray-400">{t("取消")}</button>
                </div>
              )}
            </div>
          </div>
        </div>
      </header>

      <div className="px-6 py-5">
        {/* Full-screen loading on initial load */}
        {loading && !pendingData && !ipData && !doneData && (
          <div className="flex flex-col items-center justify-center py-24">
            <div className="mb-4 h-10 w-10 animate-spin rounded-full border-4 border-gray-200 border-t-black" />
            <p className="text-sm font-medium text-gray-500">正在从飞书加载工单...</p>
            <p className="mt-1 text-xs text-gray-400">首次加载可能需要几秒钟</p>
          </div>
        )}

        {/* Stats (show skeleton while loading, real data when loaded) */}
        <div className="mb-5 grid grid-cols-4 gap-3">
          {loading && !pendingData ? (
            [1,2,3,4].map((i) => (
              <div key={i} className="rounded-xl border border-gray-100 bg-white px-4 py-3">
                <div className="h-3 w-12 animate-pulse rounded bg-gray-100" />
                <div className="mt-2 h-6 w-8 animate-pulse rounded bg-gray-100" />
              </div>
            ))
          ) : (
            [
              { label: t("待处理"), value: counts.pending, color: "" },
              { label: t("进行中"), value: counts.in_progress, color: "text-blue-600" },
              { label: t("已完成"), value: counts.done, color: "text-green-600" },
              { label: t("高优先级"), value: pendingData?.high_priority ?? 0, color: "text-red-600" },
            ].map((s) => (
              <div key={s.label} className="rounded-xl border border-gray-100 bg-white px-4 py-3">
                <p className="text-xs text-gray-400">{s.label}</p>
                <p className={`mt-0.5 text-xl font-bold ${s.color}`}>{s.value}</p>
              </div>
            ))
          )}
        </div>

        {error && <div className="mb-4 rounded-lg bg-red-50 px-4 py-3 text-sm text-red-600">{error}</div>}

        {/* Tabs */}
        <div className="mb-4 flex items-center gap-1 rounded-lg bg-gray-100 p-1 w-fit">
          {([
            { key: "pending" as Tab, label: t("待处理"), count: counts.pending },
            { key: "in_progress" as Tab, label: t("进行中"), count: counts.in_progress },
            { key: "done" as Tab, label: t("已完成"), count: counts.done },
          ]).map((t) => (
            <button key={t.key} onClick={() => setTab(t.key)} className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${tab === t.key ? "bg-white text-gray-900 shadow-sm" : "text-gray-500 hover:text-gray-700"}`}>
              {t.label}{t.count > 0 && <span className="ml-1.5 text-[11px] tabular-nums text-gray-400">{t.count}</span>}
            </button>
          ))}
        </div>

        {/* ===== PENDING TAB ===== */}
        {tab === "pending" && (
          <>
            <div className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
              <table className="min-w-full">
                <thead><tr className="border-b border-gray-100 bg-gray-50/50">
                  <th className="w-10 px-4 py-2.5"><input type="checkbox" className="rounded border-gray-300" onChange={(e) => setSelected(e.target.checked ? new Set((pendingData?.issues || []).map((i) => i.record_id)) : new Set())} /></th>
                  <th className="w-14 px-2 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">{t("级别")}</th>
                  <th className="px-4 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">{t("问题描述")}</th>
                  <th className="w-28 px-3 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">{t("设备 SN")}</th>
                  <th className="w-20 px-3 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">{t("Zendesk")}</th>
                  <th className="w-16 px-3 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">{t("飞书")}</th>
                  <th className="w-32 px-4 py-2.5 text-right text-[11px] font-semibold uppercase tracking-wider text-gray-400">{t("操作")}</th>
                </tr></thead>
                <tbody className="divide-y divide-gray-50">
                  {loading && !pendingData ? <tr><td colSpan={7} className="px-4 py-16 text-center text-sm text-gray-300">加载中...</td></tr>
                  : !pendingData?.issues.length ? <tr><td colSpan={7} className="px-4 py-16 text-center text-sm text-gray-300">{t("暂无待处理工单")}</td></tr>
                  : pendingData.issues.map((issue) => (
                    <tr key={issue.record_id} className="cursor-pointer hover:bg-gray-50/50" onClick={() => openDetail(issue.record_id, "pending")}>
                      <td className="px-4 py-3" onClick={(e) => e.stopPropagation()}><input type="checkbox" className="rounded border-gray-300" checked={selected.has(issue.record_id)} onChange={() => toggle(issue.record_id)} /></td>
                      <td className="px-2 py-3 align-top"><PriorityBadge p={issue.priority} /></td>
                      <td className="max-w-md px-4 py-3"><p className="text-sm leading-snug text-gray-800" style={{ display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>{issue.description}</p></td>
                      <td className="px-3 py-3 align-top font-mono text-xs text-gray-400">{issue.device_sn || "—"}</td>
                      <td className="px-3 py-3 align-top text-xs">{issue.zendesk_id ? <a href={issue.zendesk} target="_blank" onClick={(e) => e.stopPropagation()} className="font-medium text-blue-600 hover:underline">{issue.zendesk_id}</a> : <span className="text-gray-300">null</span>}</td>
                      <td className="px-3 py-3 align-top text-xs"><a href={issue.feishu_link} target="_blank" onClick={(e) => e.stopPropagation()} className="text-blue-500 hover:underline">{t("链接")}</a></td>
                      <td className="px-4 py-3 align-top text-right" onClick={(e) => e.stopPropagation()}>
                        <button onClick={() => startAnalysis(issue.record_id)} className="rounded-md bg-black px-3 py-1 text-xs font-medium text-white hover:bg-gray-800">{t("分析")}</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Pagination page={pendingPage} totalPages={pendingData?.total_pages ?? 1} onChange={onPendingPage} />
          </>
        )}

        {/* ===== IN_PROGRESS / DONE TABS ===== */}
        {(tab === "in_progress" || tab === "done") && (() => {
          const data = tab === "in_progress" ? ipData : doneData;
          const items = data?.issues || [];
          const currentPage = tab === "in_progress" ? ipPage : donePage;
          const onPageChange = tab === "in_progress" ? onIpPage : onDonePage;
          const emptyMsg = tab === "in_progress" ? t("暂无进行中工单") : t("暂无已完成工单");

          return (
            <>
              <div className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
                <table className="min-w-full">
                  <thead><tr className="border-b border-gray-100 bg-gray-50/50">
                    <th className="w-14 px-2 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">级别</th>
                    <th className="px-4 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">问题描述</th>
                    <th className="w-20 px-3 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">{t("提交人")}</th>
                    <th className="w-28 px-3 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">{t("创建时间")}</th>
                    <th className="w-20 px-3 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">Zendesk</th>
                    <th className="w-28 px-3 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">{t("状态")}</th>
                    <th className="w-32 px-4 py-2.5 text-right text-[11px] font-semibold uppercase tracking-wider text-gray-400">操作</th>
                  </tr></thead>
                  <tbody className="divide-y divide-gray-50">
                    {loading && !data ? <tr><td colSpan={7} className="px-4 py-16 text-center text-sm text-gray-300">加载中...</td></tr>
                    : !items.length ? <tr><td colSpan={7} className="px-4 py-16 text-center text-sm text-gray-300">{emptyMsg}</td></tr>
                    : items.map((item) => (
                      <tr key={item.record_id} className="cursor-pointer hover:bg-gray-50/50" onClick={() => openDetail(item.record_id, tab)}>
                        <td className="px-2 py-3 align-top"><PriorityBadge p={item.priority} /></td>
                        <td className="max-w-md px-4 py-3">
                          <p className="text-sm leading-snug text-gray-800" style={{ display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>{item.description}</p>
                          {(item.root_cause_summary || item.result_summary) && (
                            <div className="mt-2 space-y-1 rounded-md bg-gray-50 px-2.5 py-2">
                              {item.root_cause_summary && <div className="flex items-start gap-1.5"><span className="mt-px flex-shrink-0 text-[10px] font-semibold text-amber-600">原因</span><p className="text-xs text-gray-600" style={{ display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>{item.root_cause_summary}</p></div>}
                              {item.result_summary && <div className="flex items-start gap-1.5"><span className="mt-px flex-shrink-0 text-[10px] font-semibold text-green-600">结果</span><p className="text-xs text-gray-600" style={{ display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>{item.result_summary}</p></div>}
                            </div>
                          )}
                        </td>
                        <td className="px-3 py-3 align-top text-xs text-gray-600">{(item as any).created_by || "—"}</td>
                        <td className="px-3 py-3 align-top text-xs text-gray-400">{(item as any).created_at ? (item as any).created_at.slice(0, 16).replace("T", " ") : "—"}</td>
                        <td className="px-3 py-3 align-top text-xs">{item.zendesk_id ? <a href={item.zendesk} target="_blank" onClick={(e) => e.stopPropagation()} className="font-medium text-blue-600 hover:underline">{item.zendesk_id}</a> : <span className="text-gray-300">—</span>}</td>
                        <td className="px-3 py-3 align-top"><LocalStatusBadge item={item} /></td>
                        <td className="px-4 py-3 align-top text-right" onClick={(e) => e.stopPropagation()}>
                          <div className="flex items-center justify-end gap-1.5">
                            {item.local_status === "failed" && (
                              <button onClick={() => startAnalysis(item.record_id)} className="rounded-md bg-black px-3 py-1 text-xs font-medium text-white hover:bg-gray-800">{t("重试分析")}</button>
                            )}
                            {item.analysis?.user_reply && <button onClick={() => copy(item.analysis!.user_reply)} className="rounded-md bg-green-600 px-3 py-1 text-xs font-medium text-white hover:bg-green-700">{t("复制回复")}</button>}
                            <button onClick={() => handleEscalate(item.record_id)} className="rounded-md border border-amber-300 bg-amber-50 px-2.5 py-1 text-[11px] font-medium text-amber-700 hover:bg-amber-100" title="转工程师">{t("转工程师")}</button>
                            <button onClick={() => handleDelete(item.record_id)} className="rounded-md border border-gray-200 px-2 py-1 text-[11px] text-gray-400 hover:border-red-300 hover:text-red-500" title="删除">
                              <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}><path strokeLinecap="round" strokeLinejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" /></svg>
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

      {/* Detail panel */}
      {detailId && detailData && (
        <div className="fixed inset-0 z-50 flex">
          <div className="flex-1 bg-black/20" onClick={() => setDetailId(null)} />
          <div className="w-[520px] flex-shrink-0 overflow-y-auto border-l border-gray-200 bg-white shadow-2xl">
            <div className="sticky top-0 z-10 flex items-center justify-between border-b border-gray-100 bg-white px-5 py-3">
              <h2 className="text-sm font-semibold text-gray-800">{t("工单详情")}</h2>
              <button onClick={() => setDetailId(null)} className="rounded-lg p-1 text-gray-400 hover:bg-gray-100"><svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" /></svg></button>
            </div>
            <div className="p-5 space-y-5">
              <section>
                <div className="flex items-center gap-2 mb-3">
                  <PriorityBadge p={detailData.issue.priority} />
                  {detailData.localItem && <LocalStatusBadge item={detailData.localItem} />}
                  {detailData.issue.zendesk_id && <a href={detailData.issue.zendesk} target="_blank" className="text-xs font-medium text-blue-600 hover:underline">{detailData.issue.zendesk_id}</a>}
                  {detailData.issue.feishu_link ? (
                    <a href={detailData.issue.feishu_link} target="_blank" className="text-xs text-blue-500 hover:underline">飞书</a>
                  ) : (
                    <span className="text-xs text-gray-400">本地上传</span>
                  )}
                </div>
                <div className="grid grid-cols-2 gap-2 text-xs">
                  {[{ l: "设备 SN", v: detailData.issue.device_sn, m: true }, { l: "固件", v: detailData.issue.firmware }, { l: "APP", v: detailData.issue.app_version }, { l: "日志", v: `${detailData.issue.log_files?.length || 0} 个` }].map((f) => (
                    <div key={f.l} className="rounded-lg bg-gray-50 px-3 py-2"><span className="text-gray-400">{f.l}</span><p className={`mt-0.5 font-medium text-gray-700 ${f.m ? "font-mono" : ""}`}>{f.v || "—"}</p></div>
                  ))}
                </div>
              </section>
              <section>
                <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-gray-400">问题描述</h3>
                <div className="whitespace-pre-wrap rounded-lg bg-gray-50 p-3 text-sm leading-relaxed text-gray-700">{detailData.issue.description || "无"}</div>
              </section>

              {/* Action button for pending issues */}
              {detailTab === "pending" && !detailData.task && !detailData.result && (
                <button onClick={() => { startAnalysis(detailId!); setDetailId(null); }} className="w-full rounded-lg bg-black py-2.5 text-sm font-medium text-white hover:bg-gray-800">{t("开始 AI 分析")}</button>
              )}

              {/* Progress */}
              {detailData.task && typeof detailData.task === "object" && "status" in detailData.task && !["done","failed"].includes(detailData.task.status) && (
                <div className="rounded-lg border border-gray-100 bg-gray-50 p-4">
                  <div className="mb-2 flex justify-between text-xs text-gray-500"><span>{detailData.task.message}</span><span>{detailData.task.progress}%</span></div>
                  <div className="h-2 rounded-full bg-gray-200"><div className="h-full rounded-full bg-black transition-all duration-700" style={{ width: `${detailData.task.progress}%` }} /></div>
                </div>
              )}

              {/* Failed */}
              {detailData.task && typeof detailData.task === "object" && detailData.task.status === "failed" && (
                <>
                  <div className="rounded-lg border border-red-200 bg-red-50 p-3">
                    <p className="text-sm font-medium text-red-700">分析失败</p>
                    <p className="mt-1 text-xs text-red-500">{detailData.task.error}</p>
                  </div>
                  <button onClick={() => { startAnalysis(detailId!); setDetailId(null); }} className="w-full rounded-lg bg-black py-2.5 text-sm font-medium text-white hover:bg-gray-800">{t("重新分析")}</button>
                </>
              )}

              {/* Result */}
              {detailData.result && (() => {
                const r = detailData.result;
                const problemType = lang === "en" && r.problem_type_en ? r.problem_type_en : r.problem_type;
                const rootCause = lang === "en" && r.root_cause_en ? r.root_cause_en : r.root_cause;
                const userReply = lang === "en" && r.user_reply_en ? r.user_reply_en : r.user_reply;
                return (
                <>
                  <section>
                    <div className="mb-2 flex items-center justify-between">
                      <h3 className="text-xs font-semibold uppercase tracking-wider text-gray-400">{lang === "cn" ? "AI 分析结果" : "AI Analysis"}</h3>
                      <div className="flex items-center gap-0.5 rounded-md bg-gray-100 p-0.5">
                        <button onClick={() => setLang("cn")} className={`rounded px-2 py-0.5 text-[11px] font-medium ${lang === "cn" ? "bg-white text-gray-800 shadow-sm" : "text-gray-400"}`}>中文</button>
                        <button onClick={() => setLang("en")} className={`rounded px-2 py-0.5 text-[11px] font-medium ${lang === "en" ? "bg-white text-gray-800 shadow-sm" : "text-gray-400"}`}>EN</button>
                      </div>
                    </div>
                    <div className="flex flex-wrap gap-2 mb-3">
                      <span className="rounded-lg bg-gray-100 px-2.5 py-1 text-xs font-semibold text-gray-700">{problemType}</span>
                      <ConfBadge c={r.confidence} />
                      {r.needs_engineer && <span className="rounded-lg bg-amber-100 px-2.5 py-1 text-xs font-semibold text-amber-700">{lang === "cn" ? "需工程师" : "Engineer needed"}</span>}
                    </div>
                  </section>
                  <section>
                    <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-gray-400">{lang === "cn" ? "问题原因" : "Root Cause"}</h3>
                    <div className="whitespace-pre-wrap rounded-lg bg-gray-50 p-3 text-sm text-gray-700">{rootCause}</div>
                  </section>
                  {r.key_evidence && r.key_evidence.length > 0 && (
                    <section><h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-gray-400">{lang === "cn" ? "关键证据" : "Key Evidence"}</h3><div className="space-y-1">{r.key_evidence.map((ev, i) => <div key={i} className="rounded bg-gray-50 px-3 py-1.5 font-mono text-[11px] text-gray-600">{ev}</div>)}</div></section>
                  )}
                  {userReply && (
                    <section>
                      <div className="mb-1.5 flex items-center justify-between">
                        <h3 className="text-xs font-semibold uppercase tracking-wider text-gray-400">{lang === "cn" ? "建议回复" : "Suggested Reply"}</h3>
                        <button onClick={() => copy(userReply)} className="rounded-md bg-green-600 px-3 py-1 text-[11px] font-medium text-white hover:bg-green-700">{lang === "cn" ? "一键复制" : "Copy"}</button>
                      </div>
                      <div className="whitespace-pre-wrap rounded-lg border border-green-200 bg-green-50/50 p-3 text-sm text-gray-700">{userReply}</div>
                    </section>
                  )}
                </>
                );
              })()}

              {/* Retry button for failed analysis */}
              {detailData.localItem?.local_status === "failed" && (
                <section>
                  <button onClick={() => { startAnalysis(detailId!); setDetailId(null); }} className="w-full rounded-lg bg-black py-2.5 text-sm font-medium text-white hover:bg-gray-800">
                    重新分析
                  </button>
                </section>
              )}

              {/* Escalate to engineer button — always visible */}
              <section className="border-t border-gray-100 pt-4">
                <button
                  onClick={async () => {
                    try {
                      await escalateIssue(detailId!, "用户手动转工程师");
                      setToast("已通知值班工程师");
                    } catch (e: any) {
                      setToast(`通知失败: ${e.message}`);
                    }
                  }}
                  className="w-full rounded-lg border border-amber-300 bg-amber-50 py-2.5 text-sm font-medium text-amber-700 transition-colors hover:bg-amber-100"
                >
                  转工程师处理
                </button>
                <p className="mt-1 text-center text-[11px] text-gray-400">{t("通过飞书消息通知当前值班工程师")}</p>
              </section>
            </div>
          </div>
        </div>
      )}

      {/* First-time username setup dialog */}
      {showUsernameSetup && (
        <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/40 backdrop-blur-sm">
          <div className="w-full max-w-sm rounded-2xl bg-white p-6 shadow-2xl">
            <div className="mb-4 text-center">
              <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-black">
                <span className="text-lg font-bold text-white">J</span>
              </div>
              <h3 className="text-base font-semibold text-gray-900">{t("欢迎使用 Jarvis")}</h3>
              <p className="mt-1 text-sm text-gray-500">{t("请设置您的用户名，用于标记工单操作")}</p>
            </div>
            <input
              autoFocus
              value={usernameInput}
              onChange={(e) => setUsernameInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && usernameInput.trim()) saveUsername(usernameInput); }}
              placeholder="输入您的名字"
              className="mb-4 w-full rounded-lg border border-gray-200 px-4 py-2.5 text-center text-sm outline-none focus:border-black focus:ring-1 focus:ring-black"
            />
            <button
              onClick={() => saveUsername(usernameInput)}
              disabled={!usernameInput.trim()}
              className="w-full rounded-lg bg-black py-2.5 text-sm font-semibold text-white transition-colors hover:bg-gray-800 disabled:opacity-30"
            >
              开始使用
            </button>
          </div>
        </div>
      )}

      {toast && <Toast msg={toast} onClose={() => setToast("")} />}
    </div>
  );
}
