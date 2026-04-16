"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import { useT, useLang } from "@/lib/i18n";
import { Toast } from "@/components/Toast";
import { S, PriorityBadge, SourceBadge, FeishuLinkBadge } from "@/components/IssueComponents";
import MarkdownText from "@/components/MarkdownText";
import {
  fetchPendingIssues,
  refreshIssuesCache,
  searchFeishuIssues,
  importIssueById,
  fetchCompleted,
  fetchInProgress,
  fetchInaccurate,
  markInaccurate,
  markComplete,
  escalateIssue,
  promoteToGoldenSample,
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

// ── Small components ─────────────────────────────────────────
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
type Tab = "pending" | "in_progress" | "done" | "inaccurate" | "import";
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

  const [includeInProgress, setIncludeInProgress] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [detailId, setDetailId] = useState<string | null>(null);
  const [directDetail, setDirectDetail] = useState<LocalIssueItem | null>(null);
  const siteLang = useLang();
  const [lang, setLang] = useState<"cn" | "en">(siteLang);
  const [detailTab, setDetailTab] = useState<Tab>("pending");
  const [toast, setToast] = useState("");
  const [tab, setTab] = useState<Tab>("done");

  // Import state
  const [importQuery, setImportQuery] = useState("");
  const [importLoading, setImportLoading] = useState(false);
  const [importResults, setImportResults] = useState<Issue[] | null>(null);
  const [importProgress, setImportProgress] = useState<{ issueId: string; step: "importing" | "analyzing" | "done"; description: string } | null>(null);

  // Escalation state
  const [showEscalateDialog, setShowEscalateDialog] = useState(false);
  const [escalateNote, setEscalateNote] = useState("");
  const [escalateLoading, setEscalateLoading] = useState(false);
  const [escalateLinks, setEscalateLinks] = useState<Record<string, string>>({});

  // Follow-up state
  const [issueAnalyses, setIssueAnalyses] = useState<Record<string, AnalysisResult[]>>({});
  const [followupText, setFollowupText] = useState("");
  const [followupSubmitting, setFollowupSubmitting] = useState(false);
  const [collapsedEvidence, setCollapsedEvidence] = useState<Record<string, boolean>>({});

  useEffect(() => {
    const urlTab = new URLSearchParams(window.location.search).get("tab");
    if (urlTab === "import" || urlTab === "in_progress" || urlTab === "done" || urlTab === "inaccurate") setTab(urlTab);
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

  const loadPending = useCallback(async (page: number, withInProgress?: boolean) => {
    if (assignee === null) return;
    setPendingLoading(true);
    try { const d = await fetchPendingIssues(assignee || undefined, page, PAGE_SIZE, withInProgress ?? includeInProgress); setPendingData(d); }
    catch (e: any) { setError(e.message); }
    finally { setPendingLoading(false); }
  }, [assignee, includeInProgress]);

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
  const handleImportSearch = async () => {
    const q = importQuery.trim();
    if (!q) return;
    setImportLoading(true);
    setImportResults(null);
    try {
      const res = await searchFeishuIssues(q);
      setImportResults(res.issues || []);
    } catch (e: any) { setToast(e.message || t("搜索失败")); }
    finally { setImportLoading(false); }
  };
  const handleImportSelect = async (issue: Issue) => {
    setImportLoading(true);
    setImportProgress({ issueId: issue.record_id, step: "importing", description: issue.description || "" });
    try {
      await importIssueById(issue.record_id);
      setImportProgress({ issueId: issue.record_id, step: "analyzing", description: issue.description || "" });
      // Auto-start analysis
      const task = await createTask(issue.record_id, undefined, username || "");
      setActiveTasks((p) => ({ ...p, [issue.record_id]: task }));
      await loadInProgress(1);
      setTab("in_progress");
      setImportProgress(null); setImportQuery(""); setImportResults(null);
      setToast(t("导入成功，已开始分析"));
      // Subscribe to progress
      subscribeTaskProgress(task.task_id, (progress) => {
        setActiveTasks((p) => ({ ...p, [issue.record_id]: progress }));
        if (progress.status === "done") {
          fetchTaskResult(task.task_id).then((r) => {
            setActiveResults((p) => ({ ...p, [issue.record_id]: r }));
            loadInProgress(1); loadDone(1);
          }).catch(console.error);
        }
        if (progress.status === "failed") {
          setToast(`${t("分析失败")}: ${progress.error || t("未知错误")}`);
          loadInProgress(1); loadDone(donePage);
        }
      });
    } catch (e: any) {
      setToast(e.message || t("导入失败"));
      setImportProgress(null);
    } finally { setImportLoading(false); }
  };
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
      // Refresh both lists so the issue moves from done→in_progress
      await Promise.all([loadInProgress(1), loadDone(donePage)]);
      if (!isRetry) setTab("in_progress");
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
          loadInProgress(1); loadDone(donePage); loadPending(pendingPage);
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

  const handleEscalate = async (issueId: string) => {
    if (escalateLoading) return;
    setEscalateLoading(true);
    try {
      const res = await escalateIssue(issueId, escalateNote, username || "");
      setToast(t("已转交工程师"));
      setShowEscalateDialog(false);
      setEscalateNote("");
      // Store share_link for this issue so "已转交" section can show "打开飞书群"
      if (res.share_link) {
        setEscalateLinks(prev => ({ ...prev, [issueId]: res.share_link! }));
      }
      // Reload but don't close detail — let user see the updated state
      loadDone(donePage);
      loadInProgress(ipPage);
    } catch (e: any) { setToast(`${t("转交失败")}: ${e.message}`); }
    finally { setEscalateLoading(false); }
  };

  const handlePromoteToGolden = async (item: any) => {
    const analysis = item.analysis || item.result;
    if (!analysis) return;
    try {
      const analysisId = analysis.id;
      if (!analysisId) { setToast(t("失败")); return; }
      await promoteToGoldenSample(analysisId, username || "");
      setToast(t("已标记为金样本"));
    } catch (e: any) { setToast(`${t("失败")}: ${e.message}`); }
  };

  const handleMarkComplete = async (issueId: string) => {
    try {
      const res = await markComplete(issueId, username || "");
      const msg = res.feishu_synced ? t("已标记完成（飞书已同步）") : t("已标记完成");
      setToast(msg);
      closeDetail();
      loadDone(donePage);
      loadInProgress(ipPage);
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
  const closeDetail = () => { setDetailId(null); setDirectDetail(null); setFollowupText(""); setFollowupSubmitting(false); setShowEscalateDialog(false); setEscalateNote(""); setUrlParam("detail", ""); setUrlParam("dtab", ""); };

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
            { key: "import" as Tab, label: t("导入工单"), count: 0 },
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

        {/* ── IMPORT TAB ── */}
        {tab === "import" && (
          <>
            <div className="flex gap-2 mb-4">
              <input
                autoFocus value={importQuery} onChange={(e) => setImportQuery(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") handleImportSearch(); }}
                placeholder={t("输入设备 SN、问题描述关键词或 record ID")}
                className="flex-1 rounded-lg px-3 py-2.5 text-sm outline-none font-sans"
                style={{ background: S.surface, border: `1px solid ${S.border}`, color: S.text1 }} />
              <button onClick={handleImportSearch} disabled={importLoading || !importQuery.trim()}
                className="rounded-lg px-5 py-2.5 text-sm font-semibold disabled:opacity-50 shrink-0"
                style={{ background: S.accent, color: "#0A0B0E" }}>
                {importLoading ? t("搜索中...") : t("搜索")}
              </button>
            </div>

            {/* Import progress */}
            {importProgress && (
              <div className="mb-4 rounded-xl px-5 py-4" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
                <div className="flex items-center gap-3 mb-2">
                  <span className="inline-block h-4 w-4 animate-spin rounded-full border-2" style={{ borderColor: "rgba(0,0,0,0.08)", borderTopColor: S.accent }} />
                  <span className="text-sm font-medium" style={{ color: S.text1 }}>
                    {importProgress.step === "importing" ? t("正在导入...") : t("正在启动分析...")}
                  </span>
                </div>
                <p className="text-xs truncate mb-3" style={{ color: S.text3 }}>{importProgress.description}</p>
                <div className="h-1.5 rounded-full overflow-hidden" style={{ background: "rgba(0,0,0,0.06)" }}>
                  <div className="h-full rounded-full transition-all duration-500"
                    style={{ background: S.accent, width: importProgress.step === "importing" ? "40%" : "80%" }} />
                </div>
              </div>
            )}

            {/* Search results */}
            {importResults !== null && !importProgress && (
              <div className="overflow-hidden rounded-xl" style={{ border: `1px solid ${S.border}`, background: S.surface }}>
                {importResults.length === 0 ? (
                  <p className="px-4 py-16 text-center text-sm" style={{ color: S.text3 }}>{t("未找到匹配的工单")}</p>
                ) : (
                  <table className="min-w-full">
                    <TableHeader cols={[t("问题描述"), t("一句话归因"), t("设备 SN"), t("创建时间"), t("状态"), t("操作")]} />
                    <tbody>
                      {importResults.map((issue, idx) => (
                        <tr key={issue.record_id}
                          style={{ borderBottom: `1px solid ${S.borderSm}`, background: idx % 2 === 0 ? "transparent" : "rgba(0,0,0,0.01)" }}>
                          <td className="px-3 py-3 max-w-xs">
                            <p className="text-sm leading-snug" style={{ color: S.text1, display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>
                              {issue.description || "—"}
                            </p>
                          </td>
                          <td className="px-3 py-3 max-w-[180px]">
                            <p className="text-xs leading-snug" style={{ color: S.text2, display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>
                              {issue.root_cause_summary || "—"}
                            </p>
                          </td>
                          <td className={tdBase} style={{ width: "100px" }}>
                            <span className="font-mono text-xs" style={{ color: S.text3 }}>{issue.device_sn || "—"}</span>
                          </td>
                          <td className={tdBase} style={{ width: "100px" }}>
                            <span className="text-xs" style={{ color: S.text3 }}>
                              {issue.created_at_ms ? formatLocalTime(new Date(issue.created_at_ms).toISOString(), "date") : "—"}
                            </span>
                          </td>
                          <td className={tdBase} style={{ width: "72px" }}>
                            <span className="inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium"
                              style={issue.feishu_status === "in_progress"
                                ? { background: "rgba(96,165,250,0.12)", color: "#2563EB", border: "1px solid rgba(96,165,250,0.25)" }
                                : issue.feishu_status === "done"
                                  ? { background: "rgba(34,197,94,0.12)", color: "#16A34A", border: "1px solid rgba(34,197,94,0.25)" }
                                  : { background: "rgba(0,0,0,0.04)", color: "#6B7280", border: "1px solid rgba(0,0,0,0.08)" }}>
                              {issue.feishu_status === "in_progress" ? t("处理中") : issue.feishu_status === "done" ? t("已完成") : t("未处理")}
                            </span>
                          </td>
                          <td className={`${tdBase} text-right`} style={{ width: "110px" }}>
                            <button onClick={() => handleImportSelect(issue)} disabled={importLoading}
                              className="rounded-lg px-3 py-1 text-xs font-semibold transition-colors disabled:opacity-50"
                              style={{ background: S.accent, color: "#0A0B0E" }}>
                              {t("导入并分析")}
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            )}

            {/* Empty state */}
            {importResults === null && !importProgress && (
              <div className="flex flex-col items-center justify-center py-20" style={{ color: S.text3 }}>
                <svg className="h-12 w-12 mb-3 opacity-30" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z" />
                </svg>
                <p className="text-sm">{t("搜索飞书工单并导入分析")}</p>
              </div>
            )}
          </>
        )}

        {/* ── PENDING TAB ── */}
        {tab === "pending" && (
          <>
            {/* Include in_progress toggle */}
            <div className="mb-3 flex items-center gap-2">
              <label className="flex items-center gap-2 cursor-pointer select-none">
                <input type="checkbox" className="rounded" style={{ accentColor: S.accent }}
                  checked={includeInProgress}
                  onChange={(e) => { setIncludeInProgress(e.target.checked); setPendingPage(1); setSelected(new Set()); loadPending(1, e.target.checked); }} />
                <span className="text-xs" style={{ color: S.text2 }}>{t("同时显示最近 10 条处理中工单")}</span>
              </label>
            </div>
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
                  t("级别"), ...(includeInProgress ? [t("状态")] : []), t("来源"), t("问题描述"), t("设备 SN"), t("Zendesk"), t("飞书"), t("操作")
                ]} />
                <tbody>
                  {pendingLoading ? (
                    <tr><td colSpan={includeInProgress ? 9 : 8} className="px-4 py-16 text-center text-sm" style={{ color: S.text3 }}>
                      <span className="inline-flex items-center gap-2">
                        <span className="inline-block h-4 w-4 animate-spin rounded-full border-2" style={{ borderColor: "rgba(0,0,0,0.08)", borderTopColor: S.accent }} />
                        {t("正在从飞书同步...")}
                      </span>
                    </td></tr>
                  ) : !pendingData?.issues.length ? (
                    <tr><td colSpan={includeInProgress ? 9 : 8} className="px-4 py-16 text-center text-sm" style={{ color: S.text3 }}>{t("暂无待处理工单")}</td></tr>
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
                      {includeInProgress && (
                        <td className={tdBase} style={{ width: "72px" }}>
                          <span className="inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium"
                            style={issue.feishu_status === "in_progress"
                              ? { background: "rgba(96,165,250,0.12)", color: "#2563EB", border: "1px solid rgba(96,165,250,0.25)" }
                              : { background: "rgba(0,0,0,0.04)", color: "#6B7280", border: "1px solid rgba(0,0,0,0.08)" }}>
                            {issue.feishu_status === "in_progress" ? t("处理中") : t("未处理")}
                          </span>
                        </td>
                      )}
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
          // Filter out items that have an active task (retry in progress) from the done/inaccurate tabs
          const rawItems = data?.issues || [];
          const items = (tab === "done" || tab === "inaccurate")
            ? rawItems.filter(item => !activeTasks[item.record_id] || ["done", "failed"].includes(activeTasks[item.record_id]?.status))
            : rawItems;
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
                    ? <FeishuLinkBadge href={detailData.issue.feishu_link} />
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

                    {/* Chat-style conversation flow (chronological: oldest first) */}
                    {[...analyses].reverse().map((r, idx) => {
                      const chronoIdx = analyses.length - 1 - idx;
                      const isLatest = chronoIdx === 0;
                      const problemType = lang === "en" ? (r.problem_type_en || r.problem_type) : r.problem_type;
                      const rootCause = lang === "en" ? (r.root_cause_en || r.root_cause) : r.root_cause;
                      const userReply = lang === "en" ? (r.user_reply_en || r.user_reply) : r.user_reply;
                      const hasEnTranslation = !!(r.problem_type_en && r.root_cause_en);
                      const isFollowup = !!(r as any).followup_question;
                      const evidenceKey = r.task_id || `ev-${idx}`;
                      const evidenceCollapsed = collapsedEvidence[evidenceKey] !== false; // default collapsed
                      return (
                        <div key={r.task_id || idx} className="space-y-3">
                          {/* User's follow-up question — right-aligned bubble */}
                          {isFollowup && (
                            <div className="flex justify-end">
                              <div className="max-w-[85%] space-y-1">
                                <div className="rounded-2xl rounded-br-sm px-4 py-2.5 text-sm"
                                  style={{ background: "rgba(167,139,250,0.10)", color: S.text1, border: "1px solid rgba(167,139,250,0.18)" }}>
                                  {(r as any).followup_question}
                                </div>
                                {(r as any).created_at && (
                                  <div className="text-right text-[10px]" style={{ color: S.text3 }}>{formatLocalTime((r as any).created_at)}</div>
                                )}
                              </div>
                            </div>
                          )}

                          {/* AI analysis card — left-aligned */}
                          <div className="space-y-3 rounded-lg p-4"
                            style={{
                              background: S.surface,
                              border: `1px solid ${S.border}`,
                              borderLeft: isLatest ? `3px solid ${S.accent}` : `3px solid ${S.border}`,
                            }}>
                            {/* Badge row */}
                            <div className="flex items-center gap-2 flex-wrap">
                              <span className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold"
                                style={isFollowup
                                  ? { background: "rgba(167,139,250,0.12)", color: "#C4B5FD", border: "1px solid rgba(167,139,250,0.25)" }
                                  : { background: "rgba(184,146,46,0.08)", color: S.accent, border: "1px solid rgba(184,146,46,0.2)" }
                                }>
                                <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                                  <path strokeLinecap="round" strokeLinejoin="round" d="M9.75 3.104v5.714a2.25 2.25 0 0 1-.659 1.591L5 14.5M9.75 3.104c-.251.023-.501.05-.75.082m.75-.082a24.301 24.301 0 0 1 4.5 0m0 0v5.714a2.25 2.25 0 0 0 .659 1.591L19 14.5M14.25 3.104c.251.023.501.05.75.082M19 14.5l-2.47 2.47a2.25 2.25 0 0 1-1.591.659H9.061a2.25 2.25 0 0 1-1.591-.659L5 14.5m14 0H5" />
                                </svg>
                                {isFollowup ? t("追问分析") : t("初次分析")}
                              </span>
                              {!isFollowup && (r as any).created_at && (
                                <span className="text-[10px]" style={{ color: S.text3 }}>{formatLocalTime((r as any).created_at)}</span>
                              )}
                            </div>

                            {lang === "en" && !hasEnTranslation && (
                              <p className="text-[10px]" style={{ color: S.accent }}>English translation not available. Showing Chinese.</p>
                            )}
                            <div className="flex flex-wrap gap-2">
                              <span className="rounded-lg px-2.5 py-1 text-xs font-semibold" style={{ background: S.overlay, color: S.text1 }}>
                                {problemType}
                              </span>
                              <ConfBadge c={r.confidence} />
                              {r.agent_model && (
                                <span className="rounded-lg px-2.5 py-1 text-[10px] font-medium"
                                  style={{ background: "rgba(96,165,250,0.1)", color: "rgba(96,165,250,0.8)", border: "1px solid rgba(96,165,250,0.2)" }}>
                                  {r.agent_model.replace(/^claude-/, "").replace(/-\d{8}$/, "")}
                                </span>
                              )}
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
                              <div className="rounded-lg p-3 text-sm" style={{ background: S.overlay, color: S.text2 }}>
                                <MarkdownText>{rootCause}</MarkdownText>
                              </div>
                            </div>
                            {/* Collapsible evidence */}
                            {r.key_evidence && r.key_evidence.length > 0 && (
                              <div>
                                <button
                                  onClick={() => setCollapsedEvidence(prev => ({ ...prev, [evidenceKey]: !evidenceCollapsed }))}
                                  className="mb-1.5 flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wider"
                                  style={{ color: S.text3, background: "none", border: "none", cursor: "pointer", padding: 0 }}>
                                  <svg className={`h-3 w-3 transition-transform ${evidenceCollapsed ? "" : "rotate-90"}`}
                                    fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                                    <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
                                  </svg>
                                  {lang === "cn" ? "关键证据" : "Key Evidence"} ({r.key_evidence.length})
                                </button>
                                {!evidenceCollapsed && (
                                  <div className="space-y-2">
                                    {r.key_evidence.map((ev, i) => {
                                      // Split evidence into explanation + log line if pattern matches
                                      const logSep = ev.match(/^(.+?)\s*(?:——|--|→|=>|日志[:：])\s*([\s\S]+)$/);
                                      return (
                                        <div key={i} className="rounded-lg px-3 py-2 text-[11px]"
                                          style={{ background: S.overlay, border: `1px solid ${S.borderSm}` }}>
                                          {logSep ? (
                                            <>
                                              <div className="mb-1 text-xs" style={{ color: S.text2 }}>{logSep[1].trim()}</div>
                                              <div className="font-mono text-[10px] rounded px-2 py-1" style={{ background: S.surface, color: S.text3 }}>{logSep[2].trim()}</div>
                                            </>
                                          ) : (
                                            <div className="whitespace-pre-wrap" style={{ color: S.text2 }}>{ev}</div>
                                          )}
                                        </div>
                                      );
                                    })}
                                  </div>
                                )}
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
                                <div className="rounded-lg p-3 text-sm"
                                  style={{ background: S.overlay, color: S.text2, borderLeft: "2px solid rgba(34,197,94,0.4)" }}>
                                  <MarkdownText>{userReply}</MarkdownText>
                                </div>
                              </div>
                            )}
                          </div>
                        </div>
                      );
                    })}

                    {/* Follow-up progress — above input */}
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

                    {/* Follow-up input — anchored at bottom of conversation */}
                    <section className="rounded-lg p-3" style={{ background: S.overlay, border: `1px solid ${S.border}` }}>
                      <div className="flex gap-2 items-end">
                        <textarea
                          value={followupText}
                          onChange={(e) => setFollowupText(e.target.value)}
                          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && followupText.trim() && !followupSubmitting) { e.preventDefault(); startFollowup(detailId!, followupText); } }}
                          placeholder={t("请输入追问内容...")}
                          rows={1}
                          disabled={followupSubmitting}
                          className="flex-1 resize-none rounded-xl px-3 py-2 text-sm outline-none"
                          style={{ background: S.surface, border: `1px solid ${S.borderSm}`, color: S.text1, minHeight: "38px", maxHeight: "120px" }}
                        />
                        <button
                          onClick={() => startFollowup(detailId!, followupText)}
                          disabled={!followupText.trim() || followupSubmitting}
                          className="flex-shrink-0 rounded-xl p-2 transition-colors disabled:opacity-30"
                          style={{ background: S.accent, color: "#0A0B0E" }}>
                          {followupSubmitting ? (
                            <div className="h-4 w-4 animate-spin rounded-full border-2"
                              style={{ borderColor: "rgba(0,0,0,0.2)", borderTopColor: "#0A0B0E" }} />
                          ) : (
                            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                              <path strokeLinecap="round" strokeLinejoin="round" d="M6 12L3.269 3.126A59.768 59.768 0 0121.485 12 59.77 59.77 0 013.27 20.876L5.999 12zm0 0h7.5" />
                            </svg>
                          )}
                        </button>
                      </div>
                    </section>
                  </>
                );
              })()}

              {/* Escalation info + open group button */}
              {(detailData.localItem?.escalated_at || escalateLinks[detailId!]) && (
                <section className="rounded-lg p-3 space-y-2" style={{ background: S.orangeBg, border: `1px solid ${S.orangeBorder}` }}>
                  <div className="flex items-center gap-2 mb-1">
                    <span className="inline-flex rounded-full px-2 py-0.5 text-[10px] font-medium"
                      style={{ background: S.orangeBg, color: S.orange, border: `1px solid ${S.orangeBorder}` }}>
                      {t("已转交")}
                    </span>
                    {detailData.localItem?.escalated_by && (
                      <span className="text-xs" style={{ color: S.text2 }}>{t("转交人")}: {detailData.localItem.escalated_by}</span>
                    )}
                    {detailData.localItem?.escalated_at && (
                      <span className="text-[10px] ml-auto" style={{ color: S.text3 }}>{formatLocalTime(detailData.localItem.escalated_at)}</span>
                    )}
                  </div>
                  {detailData.localItem?.escalation_note && (
                    <p className="text-xs mt-1" style={{ color: S.orange }}>{t("转交备注")}: {detailData.localItem.escalation_note}</p>
                  )}
                  {escalateLinks[detailId!] && (
                    <a href={escalateLinks[detailId!]} target="_blank"
                      className="flex items-center justify-center gap-2 w-full rounded-lg py-2 text-sm font-semibold transition-colors hover:opacity-80"
                      style={{ background: S.orange, color: "#FFFFFF", textDecoration: "none" }}>
                      <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 6H5.25A2.25 2.25 0 003 8.25v10.5A2.25 2.25 0 005.25 21h10.5A2.25 2.25 0 0018 18.75V10.5m-10.5 6L21 3m0 0h-5.25M21 3v5.25" />
                      </svg>
                      {t("打开飞书群")}
                    </a>
                  )}
                </section>
              )}

              {/* Actions */}
              <section className="pt-4 space-y-2" style={{ borderTop: `1px solid ${S.border}` }}>
                {/* Mark complete — for done/failed, syncs to Feishu */}
                {(detailData.localItem?.local_status === "done" || detailData.localItem?.local_status === "failed") && (
                  <button onClick={() => handleMarkComplete(detailId!)}
                    className="w-full rounded-lg py-2.5 text-sm font-semibold flex items-center justify-center gap-2"
                    style={{ background: "rgba(34,197,94,0.12)", color: "#16A34A", border: "1px solid rgba(34,197,94,0.25)" }}>
                    <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                    </svg>
                    {t("标记完成")}
                  </button>
                )}
                {/* Escalate button — show for done/failed (not already escalated) */}
                {(detailData.localItem?.local_status === "done" || detailData.localItem?.local_status === "failed") && !detailData.localItem?.escalated_at && (
                  showEscalateDialog ? (
                    <div className="rounded-lg p-3 space-y-2" style={{ background: S.orangeBg, border: `1px solid ${S.orangeBorder}` }}>
                      <p className="text-xs font-medium" style={{ color: S.orange }}>{t("确定要将此工单转交给工程师处理吗？")}</p>
                      <textarea
                        value={escalateNote}
                        onChange={(e) => setEscalateNote(e.target.value)}
                        placeholder={t("请输入转交备注（可选）...")}
                        rows={2}
                        className="w-full resize-none rounded-md px-3 py-2 text-sm outline-none"
                        style={{ background: S.overlay, border: `1px solid ${S.borderSm}`, color: S.text1 }}
                      />
                      <div className="flex gap-2">
                        <button onClick={() => handleEscalate(detailId!)}
                          disabled={escalateLoading}
                          className="flex-1 rounded-lg py-2 text-sm font-semibold flex items-center justify-center gap-2 disabled:opacity-50"
                          style={{ background: S.orangeBg, color: S.orange, border: `1px solid ${S.orangeBorder}` }}>
                          {escalateLoading && <div className="h-3.5 w-3.5 animate-spin rounded-full border-2" style={{ borderColor: "rgba(234,88,12,0.3)", borderTopColor: S.orange }} />}
                          {escalateLoading ? t("转交中...") : t("确认转交")}
                        </button>
                        <button onClick={() => { setShowEscalateDialog(false); setEscalateNote(""); }}
                          disabled={escalateLoading}
                          className="rounded-lg px-4 py-2 text-sm font-medium disabled:opacity-30"
                          style={{ border: `1px solid ${S.border}`, color: S.text2 }}>
                          {t("取消")}
                        </button>
                      </div>
                    </div>
                  ) : (
                    <button onClick={() => setShowEscalateDialog(true)}
                      className="w-full rounded-lg py-2.5 text-sm font-semibold flex items-center justify-center gap-2"
                      style={{ background: S.orangeBg, color: S.orange, border: `1px solid ${S.orangeBorder}` }}>
                      <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
                      </svg>
                      {t("转交工程师")}
                    </button>
                  )
                )}
                <button onClick={() => {
                    const base = "https://nicebuild.feishu.cn/share/base/form/shrcnGuYEnRrbbVw4Y6evkyUDCo";
                    const params = new URLSearchParams();
                    const issue = detailData!.issue;
                    const appUrl = `${window.location.origin}/tracking?detail=${detailId}`;
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
                {detailData.localItem?.local_status === "failed" && (
                  <button onClick={() => { startAnalysis(detailId!, true); closeDetail(); }}
                    className="w-full rounded-lg py-2.5 text-sm font-semibold"
                    style={{ background: S.accent, color: "#0A0B0E" }}>
                    {t("重新分析")}
                  </button>
                )}
                {detailData.localItem?.local_status === "done" && (
                  <div className="space-y-2">
                    <button onClick={() => { handlePromoteToGolden(detailData.localItem || detailData); }}
                      className="w-full rounded-lg py-2.5 text-sm font-semibold"
                      style={{ background: S.accentBg, color: S.accent, border: "1px solid rgba(184,146,46,0.3)" }}>
                      {t("标记为金样本")}
                    </button>
                    <button
                      onClick={() => handleMarkInaccurate(detailId!)}
                      className="w-full rounded-lg py-2.5 text-sm font-medium transition-colors"
                      style={{ background: "rgba(239,68,68,0.10)", color: "#DC2626", border: "1px solid rgba(239,68,68,0.25)" }}>
                      {t("标记为不准确")}
                    </button>
                  </div>
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
