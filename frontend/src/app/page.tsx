"use client";

import { useEffect, useState, useCallback } from "react";
import {
  fetchPendingIssues,
  refreshIssuesCache,
  fetchCompleted,
  fetchInProgress,
  createTask,
  createFeedbackTask,
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
  if (task && !["done", "failed"].includes(task.status)) {
    const labels: Record<string, string> = { queued: "排队中", downloading: "下载中", decrypting: "解密中", extracting: "提取中", analyzing: "分析中" };
    return <span className="inline-flex items-center gap-1 rounded-full bg-blue-50 px-2 py-0.5 text-[11px] font-medium text-blue-600"><span className="h-1.5 w-1.5 animate-pulse rounded-full bg-blue-500" />{labels[task.status] || task.status}</span>;
  }
  if (item.local_status === "done" || item.analysis)
    return <span className="inline-flex items-center gap-1 rounded-full bg-green-50 px-2 py-0.5 text-[11px] font-medium text-green-700 ring-1 ring-green-200"><svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}><path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" /></svg>分析成功</span>;
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
type UploadAgent = "" | "codex" | "claude_code";
const PAGE_SIZE = 20;

export default function HomePage() {
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
  const [detailTab, setDetailTab] = useState<Tab>("pending");
  const [toast, setToast] = useState("");
  const [tab, setTab] = useState<Tab>("pending");
  const [showUploadModal, setShowUploadModal] = useState(false);
  const [uploadSubmitting, setUploadSubmitting] = useState(false);
  const [uploadDescription, setUploadDescription] = useState("");
  const [uploadDeviceSn, setUploadDeviceSn] = useState("");
  const [uploadFirmware, setUploadFirmware] = useState("");
  const [uploadAppVersion, setUploadAppVersion] = useState("");
  const [uploadZendesk, setUploadZendesk] = useState("");
  const [uploadAgentType, setUploadAgentType] = useState<UploadAgent>("codex");
  const [uploadFiles, setUploadFiles] = useState<File[]>([]);

  // --- Assignee (read from URL on mount, BEFORE any data fetching) ---
  const [assignee, setAssignee] = useState<string | null>(null); // null = not yet initialized
  const [assigneeInput, setAssigneeInput] = useState("");
  const [showAssigneeEdit, setShowAssigneeEdit] = useState(false);

  useEffect(() => {
    // Read assignee: URL param takes priority, then localStorage
    const fromUrl = getUrlParam("assignee");
    const fromStorage = typeof window !== "undefined" ? localStorage.getItem("jarvis_assignee") || "" : "";
    const a = fromUrl || fromStorage;
    setAssignee(a);
    setAssigneeInput(a);
    // Sync: ensure URL and localStorage are both set
    if (a) {
      setUrlParam("assignee", a);
      localStorage.setItem("jarvis_assignee", a);
    }
  }, []);

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
      const task = await createTask(issueId);
      setActiveTasks((p) => ({ ...p, [issueId]: task }));

      // Remove from pending list (optimistic) + reload in-progress
      setPendingData((prev) => prev ? { ...prev, issues: prev.issues.filter((i) => i.record_id !== issueId), total: prev.total - 1 } : prev);

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
  const copy = (text: string) => { navigator.clipboard.writeText(text); setToast("已复制到剪贴板"); };
  const onUploadFileChange = (files: FileList | null) => setUploadFiles(files ? Array.from(files).slice(0, 10) : []);

  const submitUpload = async () => {
    if (!uploadDescription.trim()) {
      setError("请填写问题描述");
      return;
    }
    if (!uploadFiles.length) {
      setError("请至少上传一个日志文件");
      return;
    }
    try {
      setUploadSubmitting(true);
      setError("");
      const task = await createFeedbackTask({
        description: uploadDescription.trim(),
        device_sn: uploadDeviceSn.trim(),
        firmware: uploadFirmware.trim(),
        app_version: uploadAppVersion.trim(),
        zendesk: uploadZendesk.trim(),
        agent_type: uploadAgentType || undefined,
        files: uploadFiles,
      });
      setActiveTasks((p) => ({ ...p, [task.issue_id]: task }));
      setShowUploadModal(false);
      setUploadDescription("");
      setUploadDeviceSn("");
      setUploadFirmware("");
      setUploadAppVersion("");
      setUploadZendesk("");
      setUploadFiles([]);
      setToast("上传成功，已开始分析");
      setTab("in_progress");
      loadInProgress(1);

      subscribeTaskProgress(task.task_id, (progress) => {
        setActiveTasks((p) => ({ ...p, [task.issue_id]: progress }));
        if (progress.status === "done") {
          fetchTaskResult(task.task_id).then((r) => {
            setActiveResults((p) => ({ ...p, [task.issue_id]: r }));
            loadInProgress(1);
            loadDone(1);
          }).catch(console.error);
        }
        if (progress.status === "failed") {
          setToast(`分析失败: ${progress.error || "未知错误"}`);
          loadInProgress(1);
        }
      });
    } catch (e: any) {
      setError(e.message);
    } finally {
      setUploadSubmitting(false);
    }
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
            <h1 className="text-lg font-semibold">工单分析</h1>
            <div className="flex items-center gap-1.5">
              {!showAssigneeEdit ? (
                <button onClick={() => setShowAssigneeEdit(true)} className="flex items-center gap-1 rounded-lg border border-gray-200 px-2.5 py-1 text-xs text-gray-500 hover:bg-gray-50">
                  <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}><path strokeLinecap="round" strokeLinejoin="round" d="M15.75 6a3.75 3.75 0 11-7.5 0 3.75 3.75 0 017.5 0zM4.501 20.118a7.5 7.5 0 0114.998 0" /></svg>
                  {assignee ? <span className="font-medium text-gray-800">{assignee}</span> : <span>全部指派人</span>}

                </button>
              ) : (
                <div className="flex items-center gap-1">
                  <input autoFocus value={assigneeInput} onChange={(e) => setAssigneeInput(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") applyAssignee(); if (e.key === "Escape") setShowAssigneeEdit(false); }} placeholder="指派人" className="w-28 rounded-md border border-gray-300 px-2 py-1 text-xs outline-none focus:border-black" />
                  <button onClick={applyAssignee} className="rounded-md bg-black px-2 py-1 text-[11px] font-medium text-white">确定</button>
                  {assignee && <button onClick={clearAssignee} className="rounded-md border border-gray-200 px-2 py-1 text-[11px] text-gray-500">清除</button>}
                  <button onClick={() => setShowAssigneeEdit(false)} className="text-[11px] text-gray-400">取消</button>
                </div>
              )}
            </div>
          </div>
          <div className="flex items-center gap-2">
            {selected.size > 0 && <button onClick={batchStart} className="rounded-lg bg-black px-4 py-1.5 text-sm font-medium text-white hover:bg-gray-800">批量分析 ({selected.size})</button>}
            <button onClick={() => setShowUploadModal(true)} className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm font-medium text-gray-600 hover:bg-gray-50">用户反馈上传</button>
            <button onClick={loadAll} disabled={loading} className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm font-medium text-gray-600 hover:bg-gray-50 disabled:opacity-50">{loading ? "加载中..." : "刷新"}</button>
            <button onClick={forceRefresh} disabled={loading} className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm font-medium text-gray-500 hover:bg-gray-50 disabled:opacity-50">同步飞书</button>
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
              { label: "待处理", value: counts.pending, color: "" },
              { label: "进行中", value: counts.in_progress, color: "text-blue-600" },
              { label: "已完成", value: counts.done, color: "text-green-600" },
              { label: "高优先级", value: pendingData?.high_priority ?? 0, color: "text-red-600" },
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
            { key: "pending" as Tab, label: "待处理", count: counts.pending },
            { key: "in_progress" as Tab, label: "进行中", count: counts.in_progress },
            { key: "done" as Tab, label: "已完成", count: counts.done },
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
                  <th className="w-14 px-2 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">级别</th>
                  <th className="px-4 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">问题描述</th>
                  <th className="w-28 px-3 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">设备 SN</th>
                  <th className="w-20 px-3 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">Zendesk</th>
                  <th className="w-16 px-3 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">飞书</th>
                  <th className="w-32 px-4 py-2.5 text-right text-[11px] font-semibold uppercase tracking-wider text-gray-400">操作</th>
                </tr></thead>
                <tbody className="divide-y divide-gray-50">
                  {loading && !pendingData ? <tr><td colSpan={7} className="px-4 py-16 text-center text-sm text-gray-300">加载中...</td></tr>
                  : !pendingData?.issues.length ? <tr><td colSpan={7} className="px-4 py-16 text-center text-sm text-gray-300">暂无待处理工单</td></tr>
                  : pendingData.issues.map((issue) => (
                    <tr key={issue.record_id} className="cursor-pointer hover:bg-gray-50/50" onClick={() => openDetail(issue.record_id, "pending")}>
                      <td className="px-4 py-3" onClick={(e) => e.stopPropagation()}><input type="checkbox" className="rounded border-gray-300" checked={selected.has(issue.record_id)} onChange={() => toggle(issue.record_id)} /></td>
                      <td className="px-2 py-3 align-top"><PriorityBadge p={issue.priority} /></td>
                      <td className="max-w-md px-4 py-3"><p className="text-sm leading-snug text-gray-800" style={{ display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>{issue.description}</p></td>
                      <td className="px-3 py-3 align-top font-mono text-xs text-gray-400">{issue.device_sn || "—"}</td>
                      <td className="px-3 py-3 align-top text-xs">{issue.zendesk_id ? <a href={issue.zendesk} target="_blank" onClick={(e) => e.stopPropagation()} className="font-medium text-blue-600 hover:underline">{issue.zendesk_id}</a> : <span className="text-gray-300">null</span>}</td>
                      <td className="px-3 py-3 align-top text-xs"><a href={issue.feishu_link} target="_blank" onClick={(e) => e.stopPropagation()} className="text-blue-500 hover:underline">链接</a></td>
                      <td className="px-4 py-3 align-top text-right" onClick={(e) => e.stopPropagation()}>
                        <button onClick={() => startAnalysis(issue.record_id)} className="rounded-md bg-black px-3 py-1 text-xs font-medium text-white hover:bg-gray-800">分析</button>
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
          const emptyMsg = tab === "in_progress" ? "暂无进行中工单" : "暂无已完成工单";

          return (
            <>
              <div className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
                <table className="min-w-full">
                  <thead><tr className="border-b border-gray-100 bg-gray-50/50">
                    <th className="w-14 px-2 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">级别</th>
                    <th className="px-4 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">问题描述</th>
                    <th className="w-28 px-3 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">设备 SN</th>
                    <th className="w-20 px-3 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">Zendesk</th>
                    <th className="w-16 px-3 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">飞书</th>
                    <th className="w-28 px-3 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">状态</th>
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
                        <td className="px-3 py-3 align-top font-mono text-xs text-gray-400">{item.device_sn || "—"}</td>
                        <td className="px-3 py-3 align-top text-xs">{item.zendesk_id ? <a href={item.zendesk} target="_blank" onClick={(e) => e.stopPropagation()} className="font-medium text-blue-600 hover:underline">{item.zendesk_id}</a> : <span className="text-gray-300">null</span>}</td>
                        <td className="px-3 py-3 align-top text-xs"><a href={item.feishu_link} target="_blank" onClick={(e) => e.stopPropagation()} className="text-blue-500 hover:underline">链接</a></td>
                        <td className="px-3 py-3 align-top"><LocalStatusBadge item={item} /></td>
                        <td className="px-4 py-3 align-top text-right" onClick={(e) => e.stopPropagation()}>
                          <div className="flex items-center justify-end gap-1.5">
                            {item.local_status === "failed" && tab === "in_progress" && (
                              <button onClick={() => startAnalysis(item.record_id)} className="rounded-md bg-black px-3 py-1 text-xs font-medium text-white hover:bg-gray-800">重试</button>
                            )}
                            {item.analysis?.user_reply && <button onClick={() => copy(item.analysis!.user_reply)} className="rounded-md bg-green-600 px-3 py-1 text-xs font-medium text-white hover:bg-green-700">复制回复</button>}
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

      {/* User upload modal */}
      {showUploadModal && (
        <div className="fixed inset-0 z-40 flex items-center justify-center px-4">
          <div className="absolute inset-0 bg-black/30" onClick={() => !uploadSubmitting && setShowUploadModal(false)} />
          <div className="relative z-10 w-full max-w-2xl rounded-2xl border border-gray-200 bg-white p-5 shadow-2xl">
            <div className="mb-4 flex items-center justify-between">
              <h2 className="text-base font-semibold text-gray-900">用户反馈上传</h2>
              <button onClick={() => setShowUploadModal(false)} disabled={uploadSubmitting} className="rounded-md p-1 text-gray-400 hover:bg-gray-100">X</button>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div className="col-span-2">
                <label className="mb-1 block text-xs font-semibold uppercase tracking-wider text-gray-400">问题描述</label>
                <textarea
                  value={uploadDescription}
                  onChange={(e) => setUploadDescription(e.target.value)}
                  rows={4}
                  placeholder="请描述用户操作路径、报错提示和问题发生时间。"
                  className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm text-gray-700 outline-none focus:border-black"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs font-semibold uppercase tracking-wider text-gray-400">设备 SN</label>
                <input value={uploadDeviceSn} onChange={(e) => setUploadDeviceSn(e.target.value)} className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm outline-none focus:border-black" />
              </div>
              <div>
                <label className="mb-1 block text-xs font-semibold uppercase tracking-wider text-gray-400">固件版本</label>
                <input value={uploadFirmware} onChange={(e) => setUploadFirmware(e.target.value)} className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm outline-none focus:border-black" />
              </div>
              <div>
                <label className="mb-1 block text-xs font-semibold uppercase tracking-wider text-gray-400">App 版本</label>
                <input value={uploadAppVersion} onChange={(e) => setUploadAppVersion(e.target.value)} className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm outline-none focus:border-black" />
              </div>
              <div>
                <label className="mb-1 block text-xs font-semibold uppercase tracking-wider text-gray-400">Zendesk 链接/ID</label>
                <input value={uploadZendesk} onChange={(e) => setUploadZendesk(e.target.value)} className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm outline-none focus:border-black" />
              </div>
              <div>
                <label className="mb-1 block text-xs font-semibold uppercase tracking-wider text-gray-400">分析引擎</label>
                <select value={uploadAgentType} onChange={(e) => setUploadAgentType(e.target.value as UploadAgent)} className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm outline-none focus:border-black">
                  <option value="codex">codex</option>
                  <option value="claude_code">claude_code</option>
                </select>
              </div>
              <div className="col-span-2">
                <label className="mb-1 block text-xs font-semibold uppercase tracking-wider text-gray-400">日志文件（最多 10 个）</label>
                <input type="file" multiple onChange={(e) => onUploadFileChange(e.target.files)} className="block w-full text-sm text-gray-600 file:mr-3 file:rounded-md file:border-0 file:bg-gray-100 file:px-3 file:py-1.5 file:text-xs file:font-medium file:text-gray-700 hover:file:bg-gray-200" />
                {uploadFiles.length > 0 && (
                  <div className="mt-2 max-h-28 space-y-1 overflow-auto rounded-lg bg-gray-50 p-2">
                    {uploadFiles.map((f, idx) => (
                      <div key={`${f.name}_${idx}`} className="text-xs text-gray-600">{f.name}</div>
                    ))}
                  </div>
                )}
              </div>
            </div>
            <div className="mt-5 flex items-center justify-end gap-2">
              <button onClick={() => setShowUploadModal(false)} disabled={uploadSubmitting} className="rounded-lg border border-gray-200 px-3 py-2 text-sm text-gray-600 hover:bg-gray-50 disabled:opacity-60">取消</button>
              <button onClick={submitUpload} disabled={uploadSubmitting} className="rounded-lg bg-black px-4 py-2 text-sm font-medium text-white hover:bg-gray-800 disabled:opacity-60">{uploadSubmitting ? "上传并分析中..." : "上传并开始分析"}</button>
            </div>
          </div>
        </div>
      )}

      {/* Detail panel */}
      {detailId && detailData && (
        <div className="fixed inset-0 z-50 flex">
          <div className="flex-1 bg-black/20" onClick={() => setDetailId(null)} />
          <div className="w-[520px] flex-shrink-0 overflow-y-auto border-l border-gray-200 bg-white shadow-2xl">
            <div className="sticky top-0 z-10 flex items-center justify-between border-b border-gray-100 bg-white px-5 py-3">
              <h2 className="text-sm font-semibold text-gray-800">工单详情</h2>
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
                <button onClick={() => { startAnalysis(detailId!); setDetailId(null); }} className="w-full rounded-lg bg-black py-2.5 text-sm font-medium text-white hover:bg-gray-800">开始 AI 分析</button>
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
                <div className="rounded-lg border border-red-200 bg-red-50 p-3"><p className="text-sm font-medium text-red-700">分析失败</p><p className="mt-1 text-xs text-red-500">{detailData.task.error}</p></div>
              )}

              {/* Result */}
              {detailData.result && (
                <>
                  <section>
                    <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-gray-400">AI 分析结果</h3>
                    <div className="flex flex-wrap gap-2 mb-3">
                      <span className="rounded-lg bg-gray-100 px-2.5 py-1 text-xs font-semibold text-gray-700">{detailData.result.problem_type}</span>
                      <ConfBadge c={detailData.result.confidence} />
                      {detailData.result.needs_engineer && <span className="rounded-lg bg-amber-100 px-2.5 py-1 text-xs font-semibold text-amber-700">需工程师</span>}
                    </div>
                  </section>
                  <section><h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-gray-400">问题原因</h3><div className="whitespace-pre-wrap rounded-lg bg-gray-50 p-3 text-sm text-gray-700">{detailData.result.root_cause}</div></section>
                  {detailData.result.key_evidence && detailData.result.key_evidence.length > 0 && (
                    <section><h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-gray-400">关键证据</h3><div className="space-y-1">{detailData.result.key_evidence.map((ev, i) => <div key={i} className="rounded bg-gray-50 px-3 py-1.5 font-mono text-[11px] text-gray-600">{ev}</div>)}</div></section>
                  )}
                  {detailData.result.core_logs && detailData.result.core_logs.length > 0 && (
                    <section>
                      <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-gray-400">核心日志</h3>
                      <div className="space-y-1">{detailData.result.core_logs.map((line, i) => <div key={i} className="rounded bg-gray-50 px-3 py-1.5 font-mono text-[11px] text-gray-600">{line}</div>)}</div>
                    </section>
                  )}
                  {detailData.result.code_locations && detailData.result.code_locations.length > 0 && (
                    <section>
                      <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-gray-400">代码定位</h3>
                      <div className="space-y-1">{detailData.result.code_locations.map((loc, i) => <div key={i} className="rounded bg-blue-50 px-3 py-1.5 font-mono text-[11px] text-blue-700">{loc}</div>)}</div>
                    </section>
                  )}
                  {(detailData.result.requires_more_info || detailData.result.more_info_guidance) && (
                    <section>
                      <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-gray-400">补充信息要求</h3>
                      <div className="whitespace-pre-wrap rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">{detailData.result.more_info_guidance || "需要用户补充更多信息后再分析。"}</div>
                    </section>
                  )}
                  {detailData.result.next_steps && detailData.result.next_steps.length > 0 && (
                    <section>
                      <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-gray-400">下一步操作</h3>
                      <div className="space-y-1">{detailData.result.next_steps.map((step, i) => <div key={i} className="rounded bg-gray-50 px-3 py-2 text-sm text-gray-700">{i + 1}. {step}</div>)}</div>
                    </section>
                  )}
                  {detailData.result.user_reply && (
                    <section>
                      <div className="mb-1.5 flex items-center justify-between"><h3 className="text-xs font-semibold uppercase tracking-wider text-gray-400">建议回复</h3><button onClick={() => copy(detailData.result!.user_reply)} className="rounded-md bg-green-600 px-3 py-1 text-[11px] font-medium text-white hover:bg-green-700">一键复制</button></div>
                      <div className="whitespace-pre-wrap rounded-lg border border-green-200 bg-green-50/50 p-3 text-sm text-gray-700">{detailData.result.user_reply}</div>
                    </section>
                  )}
                </>
              )}
            </div>
          </div>
        </div>
      )}

      {toast && <Toast msg={toast} onClose={() => setToast("")} />}
    </div>
  );
}
