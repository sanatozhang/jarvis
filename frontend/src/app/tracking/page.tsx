"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import { useT, useLang } from "@/lib/i18n";
import { Toast } from "@/components/Toast";
import { CountUp } from "@/components/CountUp";
import { S, PriorityBadge, SourceBadge, FeishuLinkBadge } from "@/components/IssueComponents";
import { AnalysisResultView } from "@/components/AnalysisResultView";
import { trackEvent } from "@/lib/track";
import { fetchTracking, markInaccurate, markComplete, escalateIssue, promoteToGoldenSample, formatLocalTime, createTask, subscribeTaskProgress, fetchIssueAnalyses, fetchIssueDetail, fetchTaskResult, type LocalIssueItem, type PaginatedResponse, type TrackingFilters, type AnalysisResult, type TaskProgress } from "@/lib/api";

// 每条同时带 key（稳定英文标识，新工单 issues.category 存的就是它）+ 长中文 label
// （老工单 issues.category 存的就是它）+ 短 label——catShort 双向 fallback 兼容。
const CATEGORIES_DATA = [
  { key: "hardware",    value: "硬件交互（蓝牙连接，固件升级，文件传输，音频播放，音频剪辑、音质不佳等）", cn: "硬件交互", en: "Hardware" },
  { key: "file_home",   value: "文件首页（首页所有功能，列表显示，移动文件夹，批量转写，重命名，合并音频，删除文件，导入音频，时钟问题导致文件名不一致）", cn: "文件首页", en: "File Home" },
  { key: "file_mgmt",   value: "文件管理（转写，总结，文件编辑，分享导出，更多菜单，ASK Plaud，PCS）", cn: "文件管理", en: "File Mgmt" },
  { key: "user_system", value: "用户系统与管理（账号登录注册，Onboarding，个人资料，偏好设置，app push 通知）", cn: "用户系统", en: "User System" },
  { key: "monetization",value: "商业化（会员购买，会员转化）", cn: "商业化", en: "Monetization" },
  { key: "other",       value: "其他通用模块（Autoflow，模版社区，Plaud WEB、集成、功能许愿池、推荐朋友、隐私与安全、帮助与支持等其他功能）", cn: "其他", en: "Other" },
  { key: "izyrec",      value: "iZYREC 硬件问题", cn: "iZYREC", en: "iZYREC" },
];
const CATEGORIES = CATEGORIES_DATA.map((c) => c.value);
const CATEGORY_SHORT: Record<string, string> = {};
const CATEGORY_SHORT_EN: Record<string, string> = {};
CATEGORIES_DATA.forEach((c) => {
  // 老数据：issues.category = 长中文串
  CATEGORY_SHORT[c.value] = c.cn;
  CATEGORY_SHORT_EN[c.value] = c.en;
  // 新数据：issues.category = 英文 key（如 "hardware"）
  CATEGORY_SHORT[c.key] = c.cn;
  CATEGORY_SHORT_EN[c.key] = c.en;
});

// 注意 \s* 必须放进 group——`[APP] [Category]` 之间有空格，否则第二个 [...]
// 不连续匹配。原版只剥了 `[APP]`，留下 `[硬件交互（…）]` 在用户面前。
function stripCategoryPrefix(desc: string): string {
  return desc.replace(/^(\[[^\]]*\]\s*)+/, "").trim() || desc;
}

function StatusBadge({ status, ruleType }: { status: string; ruleType?: string }) {
  const t = useT();
  const cfg: Record<string, { bg: string; color: string; border: string; label: string }> = {
    analyzing:  { bg: "rgba(96,165,250,0.12)",  color: "#2563EB", border: "rgba(96,165,250,0.25)",  label: t("分析中") },
    done:       { bg: "rgba(34,197,94,0.12)",   color: "#16A34A", border: "rgba(34,197,94,0.25)",   label: t("成功") },
    failed:     { bg: "rgba(239,68,68,0.12)",   color: "#DC2626", border: "rgba(239,68,68,0.25)",   label: t("失败") },
    escalated:  { bg: S.orangeBg,  color: S.orange, border: S.orangeBorder,  label: t("已转交") },
  };
  const s = cfg[status] || { bg: "var(--j-accent-soft)", color: S.text3, border: S.border, label: status };
  const ruleMatched = status === "done" && ruleType && ruleType !== "general";
  return (
    <span className="inline-flex items-center gap-1">
      <span className="inline-flex rounded-full px-2 py-0.5 text-[10px] font-medium"
        style={{ background: s.bg, color: s.color, border: `1px solid ${s.border}` }}>
        {s.label}
      </span>
      {ruleMatched && (
        <span className="inline-flex rounded-full px-1.5 py-0.5 text-[9px] font-bold"
          style={{ background: "rgba(14,124,134,0.15)", color: S.accent, border: "1px solid rgba(14,124,134,0.3)" }}>
          100%
        </span>
      )}
    </span>
  );
}

function Pagination({ page, totalPages, onChange }: { page: number; totalPages: number; onChange: (p: number) => void }) {
  const t = useT();
  if (totalPages <= 1) return null;
  return (
    <div className="mt-4 flex items-center justify-center gap-2">
      <button disabled={page <= 1} onClick={() => onChange(page - 1)}
        className="rounded-lg px-3 py-1.5 text-xs font-medium transition-colors disabled:opacity-30"
        style={{ border: `1px solid ${S.border}`, color: S.text2 }}>{t("上一页")}</button>
      <span className="text-xs tabular-nums" style={{ color: S.text3 }}>{page} / {totalPages}</span>
      <button disabled={page >= totalPages} onClick={() => onChange(page + 1)}
        className="rounded-lg px-3 py-1.5 text-xs font-medium transition-colors disabled:opacity-30"
        style={{ border: `1px solid ${S.border}`, color: S.text2 }}>{t("下一页")}</button>
    </div>
  );
}

const inputStyle = {
  background: S.overlay, border: `1px solid ${S.border}`,
  color: S.text1, outline: "none", fontSize: "12px",
};
const labelStyle = {
  display: "block", marginBottom: "4px",
  fontSize: "10px", fontWeight: 600, textTransform: "uppercase" as const,
  letterSpacing: "0.08em", color: S.text3,
};

export default function TrackingPage() {
  const t = useT();
  const currentLang = useLang();
  const catShort = (cat: string) => currentLang === "en" ? (CATEGORY_SHORT_EN[cat] || cat) : (CATEGORY_SHORT[cat] || cat);
  /** Pick the right language field — fallback to Chinese if English is empty */
  const lang = <T extends Record<string, any>>(obj: T, zhKey: string, enKey: string): string => {
    if (currentLang === "en") return (obj as any)[enKey] || (obj as any)[zhKey] || "";
    return (obj as any)[zhKey] || "";
  };

  const [data, setData] = useState<PaginatedResponse<LocalIssueItem> | null>(null);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [toast, setToast] = useState("");
  const [detailItem, setDetailItem] = useState<LocalIssueItem | null>(null);
  // Deep-analysis confirmation: holds the issue id pending confirmation, or null
  const [deepConfirmId, setDeepConfirmId] = useState<string | null>(null);
  // Mark-complete reason dialog: holds the issue id pending a reason, or null
  const [completeId, setCompleteId] = useState<string | null>(null);
  const [completeReason, setCompleteReason] = useState("");

  // Escalation state
  const [showEscalateDialog, setShowEscalateDialog] = useState(false);
  const [showFeishuTransferDialog, setShowFeishuTransferDialog] = useState(false);
  const [escalateNote, setEscalateNote] = useState("");
  const [escalateLoading, setEscalateLoading] = useState(false);
  const [escalateLinks, setEscalateLinks] = useState<Record<string, string>>({});

  // Follow-up state
  const [followupText, setFollowupText] = useState("");
  const [followupSubmitting, setFollowupSubmitting] = useState(false);
  const [issueAnalyses, setIssueAnalyses] = useState<Record<string, AnalysisResult[]>>({});
  const [activeTasks, setActiveTasks] = useState<Record<string, TaskProgress>>({});
  const [collapsedEvidence, setCollapsedEvidence] = useState<Record<string, boolean>>({});

  // Ref to hold latest load function for use in async callbacks
  const loadRef = useRef<((p: number) => Promise<void>) | null>(null);

  // Subscribe to in-progress task for an issue
  const subscribeIfAnalyzing = useCallback((item: LocalIssueItem) => {
    const taskStatus = item.task?.status;
    if (!item.task?.task_id || !taskStatus || ["done", "failed"].includes(taskStatus)) return;
    // Already tracking this issue
    if (activeTasks[item.record_id]) return;
    setActiveTasks((p) => ({ ...p, [item.record_id]: item.task as TaskProgress }));
    setFollowupSubmitting(true);
    subscribeTaskProgress(item.task.task_id, (progress) => {
      setActiveTasks((p) => ({ ...p, [item.record_id]: progress }));
      if (progress.status === "done") {
        fetchIssueAnalyses(item.record_id).then((analyses) => {
          setIssueAnalyses((prev) => ({ ...prev, [item.record_id]: analyses }));
        }).catch(() => {});
        fetchIssueDetail(item.record_id).then((updated) => {
          setDetailItem(updated);
        }).catch(() => {});
        setFollowupSubmitting(false);
        setFollowupText("");
        // Refresh list after a short delay
        setTimeout(() => { loadRef.current?.(page); }, 2000);
      }
      if (progress.status === "failed") {
        setToast(`${t("分析失败")}: ${progress.error || t("未知错误")}`);
        setFollowupSubmitting(false);
      }
    });
  }, [activeTasks, page, t]);

  const openDetail = (item: LocalIssueItem) => {
    setDetailItem(item);
    setShowEscalateDialog(false);
    setShowFeishuTransferDialog(false);
    setEscalateNote("");
    const url = new URL(window.location.href);
    url.searchParams.set("detail", item.record_id);
    window.history.replaceState({}, "", url.toString());
    // Pre-load all analyses for this issue
    fetchIssueAnalyses(item.record_id).then((analyses) => {
      setIssueAnalyses((prev) => ({ ...prev, [item.record_id]: analyses }));
    }).catch(() => {});
    // Auto-subscribe if the issue is currently being analyzed
    subscribeIfAnalyzing(item);
  };
  const closeDetail = () => {
    setDetailItem(null);
    setFollowupText("");
    setFollowupSubmitting(false);
    setShowEscalateDialog(false);
    setShowFeishuTransferDialog(false);
    setEscalateNote("");
    const url = new URL(window.location.href);
    url.searchParams.delete("detail");
    window.history.replaceState({}, "", url.toString());
  };

  // Esc closes the docked detail panel (clicking the list now switches tickets, not closes)
  useEffect(() => {
    if (!detailItem) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") closeDetail(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [detailItem]);

  const startFollowup = async (issueId: string, question: string) => {
    if (!question.trim()) return;
    setFollowupSubmitting(true);
    try {
      const task = await createTask(issueId, undefined, username || "", question.trim());
      setActiveTasks((p) => ({ ...p, [issueId]: task }));
      subscribeTaskProgress(task.task_id, (progress) => {
        setActiveTasks((p) => ({ ...p, [issueId]: progress }));
        if (progress.status === "done") {
          fetchIssueAnalyses(issueId).then((analyses) => {
            setIssueAnalyses((prev) => ({ ...prev, [issueId]: analyses }));
          }).catch(() => {});
          setFollowupSubmitting(false);
          setFollowupText("");
          setTimeout(() => load(page), 2000);
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

  const [filters, setFilters] = useState<TrackingFilters>(() => {
    if (typeof window === "undefined") return {};
    const sp = new URLSearchParams(window.location.search);
    const init: TrackingFilters = {};
    if (sp.get("created_by")) init.created_by = sp.get("created_by")!;
    if (sp.get("platform")) init.platform = sp.get("platform")!;
    if (sp.get("category")) init.category = sp.get("category")!;
    if (sp.get("status")) init.status = sp.get("status")!;
    if (sp.get("source")) init.source = sp.get("source")!;
    if (sp.get("date_from")) init.date_from = sp.get("date_from")!;
    if (sp.get("date_to")) init.date_to = sp.get("date_to")!;
    return init;
  });
  const [showFilters, setShowFilters] = useState(() => {
    if (typeof window === "undefined") return false;
    const sp = new URLSearchParams(window.location.search);
    return !!(sp.get("platform") || sp.get("category") || sp.get("status") || sp.get("source") || sp.get("date_from") || sp.get("date_to"));
  });
  const username = typeof window !== "undefined" ? localStorage.getItem("appllo_username") || "" : "";
  const activeFilterCount = Object.values(filters).filter(Boolean).length;

  const load = useCallback(async (p: number) => {
    setLoading(true);
    try { setData(await fetchTracking(p, 20, filters)); } catch {} finally { setLoading(false); }
  }, [filters]);
  loadRef.current = load;

  useEffect(() => { load(page); }, [load, page]);

  // Restore detail panel from URL ?detail= param
  const urlDetailHandled = useRef(false);
  useEffect(() => {
    if (urlDetailHandled.current) return;
    const params = new URLSearchParams(window.location.search);
    const urlDetail = params.get("detail") || params.get("issue");
    if (!urlDetail) return;
    // Wait for data to load before trying to find in current page
    if (!data) return;

    urlDetailHandled.current = true;

    const enrichDetail = (item: LocalIssueItem) => {
      setDetailItem(item);
      fetchIssueAnalyses(item.record_id).then((analyses) => {
        setIssueAnalyses((prev) => ({ ...prev, [item.record_id]: analyses }));
      }).catch(() => {});
      subscribeIfAnalyzing(item);
    };

    // Try to find in current page first
    const item = data.issues.find((i) => i.record_id === urlDetail);
    if (item) {
      enrichDetail(item);
    } else {
      // Not on current page — fetch directly via API
      fetchIssueDetail(urlDetail).then(enrichDetail).catch(() => {
        setToast(`${t("加载失败")}: ${urlDetail}`);
      });
    }
  }, [data]);

  const syncFiltersToUrl = (f: TrackingFilters) => {
    const url = new URL(window.location.href);
    const filterKeys: (keyof TrackingFilters)[] = ["created_by", "platform", "category", "status", "source", "zendesk_id", "date_from", "date_to"];
    for (const k of filterKeys) { f[k] ? url.searchParams.set(k, f[k]!) : url.searchParams.delete(k); }
    window.history.replaceState({}, "", url.toString());
  };

  const updateFilter = (key: keyof TrackingFilters, val: string) => {
    setFilters((prev) => {
      const next = { ...prev, [key]: val || undefined };
      if (!val) delete next[key];
      syncFiltersToUrl(next);
      return next;
    });
    setPage(1);
  };

  const clearFilters = () => { const f = {}; setFilters(f); syncFiltersToUrl(f); setPage(1); };
  const setMyIssues = () => { const f: TrackingFilters = { created_by: username }; setFilters(f); syncFiltersToUrl(f); setPage(1); };

  const copy = (text: string) => { navigator.clipboard.writeText(text); setToast(t("已复制到剪贴板")); };
  const handleRetry = async (issueId: string) => {
    try {
      const res = await fetch("/api/tasks", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ issue_id: issueId, username: username || "" }) });
      if (!res.ok) throw new Error(await res.text());
      setToast(t("已重新触发分析")); setTimeout(() => load(page), 2000);
    } catch (e: any) { setToast(`${t("重试失败")}: ${e.message}`); }
  };
  const handleDeepAnalysis = async (issueId: string, direction?: string) => {
    try {
      const task = await createTask(issueId, undefined, username || "", direction || undefined, true);
      setActiveTasks((p) => ({ ...p, [issueId]: task }));
      setToast(t("深度分析已启动"));
      subscribeTaskProgress(task.task_id, (progress) => {
        setActiveTasks((p) => ({ ...p, [issueId]: progress }));
        if (progress.status === "done") { setTimeout(() => load(page), 500); }
        if (progress.status === "failed") {
          setToast(`${t("分析失败")}: ${progress.error || t("未知错误")}`);
          setTimeout(() => load(page), 500);
        }
      });
    } catch (e: any) { setToast(`${t("重试失败")}: ${e.message}`); }
  };
  // Deep analysis is gated behind a confirmation dialog (high token cost / long runtime).
  const confirmDeepAnalysis = () => {
    const id = deepConfirmId;
    if (!id) return;
    setDeepConfirmId(null);
    trackEvent("deep_analysis", { issue_id: id });
    handleDeepAnalysis(id);
  };
  const handleMarkInaccurate = async (issueId: string) => {
    try { await markInaccurate(issueId); setToast(t("已标记为不准确")); setTimeout(() => load(page), 500); }
    catch (e: any) { setToast(`${t("失败")}: ${e.message}`); }
  };
  const handlePromoteToGolden = async (item: LocalIssueItem) => {
    if (!item.analysis) return;
    try {
      const analysisId = (item.analysis as any).id;
      if (!analysisId) { setToast(t("失败")); return; }
      await promoteToGoldenSample(analysisId, username);
      setToast(t("已标记为金样本"));
    } catch (e: any) { setToast(`${t("失败")}: ${e.message}`); }
  };

  const handleEscalate = async (issueId: string) => {
    if (escalateLoading) return;
    setEscalateLoading(true);
    try {
      const myEmail = typeof window !== "undefined" ? localStorage.getItem("appllo_feishu_email") || "" : "";
      const res = await escalateIssue(issueId, escalateNote, username, myEmail);
      const groupExists = (res as any).group_exists;
      setToast(groupExists ? t("已通知值周工程师（飞书群已存在）") : t("已转交工程师"));
      setShowEscalateDialog(false);
      setEscalateNote("");
      if (res.share_link) {
        setEscalateLinks(prev => ({ ...prev, [issueId]: res.share_link! }));
      }
      setTimeout(() => load(page), 500);
    } catch (e: any) { setToast(`${t("转交失败")}: ${e.message}`); }
    finally { setEscalateLoading(false); }
  };

  const handleMarkComplete = async (issueId: string, reason: string) => {
    try {
      const res = await markComplete(issueId, username, reason);
      const msg = res.feishu_notified
        ? t("工单已标记完成，已通知飞书群")
        : res.feishu_synced ? t("已标记完成（飞书已同步）") : t("已标记完成");
      setToast(msg);
      closeDetail();
      setTimeout(() => load(page), 500);
    } catch (e: any) { setToast(`${t("失败")}: ${e.message}`); }
  };

  const thStyle = { color: S.text3, fontSize: "10px", fontWeight: 600, textTransform: "uppercase" as const, letterSpacing: "0.08em", padding: "10px 12px" };
  const tdBase = "px-3 py-3 align-top";

  return (
    <div className={`min-h-full transition-[padding] duration-200 ${detailItem ? "lg:pr-[35%]" : ""}`}>
      {/* Header */}
      <header className="sticky top-0 z-10 backdrop-blur-md"
        style={{ background: "var(--j-header)", borderBottom: `1px solid ${S.border}` }}>
        <div className="flex items-center justify-between px-6 py-3">
          <h1 className="text-base font-semibold" style={{ color: S.text1 }}>{t("工单跟踪")}</h1>
          <div className="flex items-center gap-2">
            <div className="flex items-center gap-1 rounded-lg p-1" style={{ background: S.overlay }}>
              <button onClick={clearFilters}
                className="rounded-md px-3 py-1.5 text-sm font-medium transition-all"
                style={activeFilterCount === 0 ? { background: S.surface, color: S.text1 } : { color: S.text3 }}>
                {t("全部")}
              </button>
              <button onClick={setMyIssues}
                className="rounded-md px-3 py-1.5 text-sm font-medium transition-all"
                style={filters.created_by === username ? { background: S.surface, color: S.text1 } : { color: S.text3 }}>
                {t("我的")}
              </button>
            </div>
            <button onClick={() => setShowFilters(!showFilters)}
              className="rounded-lg px-3 py-1.5 text-sm font-medium transition-colors"
              style={showFilters || activeFilterCount > 0
                ? { background: S.accent, color: "#FFFFFF" }
                : { border: `1px solid ${S.border}`, color: S.text2 }}>
              {t("筛选")}{activeFilterCount > 0 && ` (${activeFilterCount})`}
            </button>
          </div>
        </div>

        {/* Filter bar */}
        {showFilters && (
          <div className="px-6 py-3" style={{ borderTop: `1px solid ${S.border}`, background: "var(--j-surface)" }}>
            <div className="flex flex-wrap items-end gap-3">
              <div className="w-32">
                <label style={labelStyle}>{t("提交人")}</label>
                <input value={filters.created_by || ""} onChange={(e) => updateFilter("created_by", e.target.value)}
                  placeholder={t("用户名")} className="w-full rounded-lg px-2.5 py-1.5 font-sans" style={inputStyle} />
              </div>
              <div className="w-28">
                <label style={labelStyle}>{t("平台")}</label>
                <select value={filters.platform || ""} onChange={(e) => updateFilter("platform", e.target.value)}
                  className="w-full rounded-lg px-2.5 py-1.5 font-sans" style={inputStyle}>
                  <option value="">{t("全部")}</option>
                  <option value="APP">APP</option>
                  <option value="Web">Web</option>
                  <option value="Desktop">Desktop</option>
                </select>
              </div>
              <div className="w-24">
                <label style={labelStyle}>{t("来源")}</label>
                <select value={filters.source || ""} onChange={(e) => updateFilter("source", e.target.value)}
                  className="w-full rounded-lg px-2.5 py-1.5 font-sans" style={inputStyle}>
                  <option value="">{t("全部")}</option>
                  <option value="feishu">{t("飞书")}</option>
                  <option value="local">{t("网站提交")}</option>
                  <option value="linear">Linear</option>
                  <option value="api">API</option>
                </select>
              </div>
              <div className="w-32">
                <label style={labelStyle}>Zendesk</label>
                <input value={filters.zendesk_id || ""} onChange={(e) => updateFilter("zendesk_id", e.target.value)}
                  placeholder={t("工单号")} className="w-full rounded-lg px-2.5 py-1.5 font-sans" style={inputStyle} />
              </div>
              <div className="w-44">
                <label style={labelStyle}>{t("问题分类")}</label>
                <select value={filters.category || ""} onChange={(e) => updateFilter("category", e.target.value)}
                  className="w-full rounded-lg px-2.5 py-1.5 font-sans" style={inputStyle}>
                  <option value="">{t("全部分类")}</option>
                  {CATEGORIES.map((c) => <option key={c} value={c}>{catShort(c)}</option>)}
                </select>
              </div>
              <div className="w-24">
                <label style={labelStyle}>{t("状态")}</label>
                <select value={filters.status || ""} onChange={(e) => updateFilter("status", e.target.value)}
                  className="w-full rounded-lg px-2.5 py-1.5 font-sans" style={inputStyle}>
                  <option value="">{t("全部")}</option>
                  <option value="analyzing">{t("分析中")}</option>
                  <option value="done">{t("成功")}</option>
                  <option value="failed">{t("失败")}</option>
                  <option value="inaccurate">{t("不准确")}</option>
                </select>
              </div>
              <div className="w-32">
                <label style={labelStyle}>{t("起始日期")}</label>
                <input type="date" value={filters.date_from || ""} onChange={(e) => updateFilter("date_from", e.target.value)}
                  className="w-full rounded-lg px-2 py-1.5 font-sans" style={inputStyle} />
              </div>
              <div className="w-32">
                <label style={labelStyle}>{t("结束日期")}</label>
                <input type="date" value={filters.date_to || ""} onChange={(e) => updateFilter("date_to", e.target.value)}
                  className="w-full rounded-lg px-2 py-1.5 font-sans" style={inputStyle} />
              </div>
              {activeFilterCount > 0 && (
                <button onClick={clearFilters} className="rounded-lg px-2.5 py-1.5 text-xs transition-colors"
                  style={{ color: "#DC2626" }}>
                  {t("清除筛选")}
                </button>
              )}
            </div>
          </div>
        )}
      </header>

      <div className="px-6 py-5">
        {data && (
          <p className="mb-3 text-xs" style={{ color: S.text3 }}>
            {t("共")} <CountUp value={Number(data.total)} /> {t("个工单")}
            {activeFilterCount > 0 && (
              <span className="ml-2 space-x-1">
                {filters.created_by && <span className="rounded px-1.5 py-0.5 text-[10px]" style={{ background: S.overlay, color: S.text2 }}>{t("提交人")}: {filters.created_by}</span>}
                {filters.platform && <span className="rounded px-1.5 py-0.5 text-[10px]" style={{ background: "rgba(96,165,250,0.1)", color: "#2563EB" }}>{filters.platform}</span>}
                {filters.category && <span className="rounded px-1.5 py-0.5 text-[10px]" style={{ background: S.accentBg, color: S.accent }}>{catShort(filters.category)}</span>}
                {filters.status && <span className="rounded px-1.5 py-0.5 text-[10px]" style={{ background: S.overlay, color: S.text2 }}>{filters.status}</span>}
                {filters.zendesk_id && <span className="rounded px-1.5 py-0.5 text-[10px]" style={{ background: "rgba(234,179,8,0.1)", color: "#B45309" }}>Zendesk: {filters.zendesk_id}</span>}
                {filters.source && <span className="rounded px-1.5 py-0.5 text-[10px]" style={{ background: "rgba(167,139,250,0.1)", color: "#7C3AED" }}>{filters.source}</span>}
              </span>
            )}
          </p>
        )}

        <div className="overflow-hidden rounded-xl" style={{ border: `1px solid ${S.border}`, background: S.surface }}>
          <table className="min-w-full">
            <thead>
              <tr style={{ borderBottom: `1px solid ${S.border}`, background: "rgba(0,0,0,0.02)" }}>
                {[t("级别"), t("问题描述"), t("状态"), t("平台"), t("来源"), t("提交人"), t("创建时间"), "Zendesk", t("操作")].map((col) => (
                  <th key={col as string} style={{ ...thStyle, textAlign: col === t("操作") ? "right" : "left" }}>{col}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {loading && !data ? (
                <tr><td colSpan={9} className="px-4 py-16 text-center text-sm" style={{ color: S.text3 }}>{t("加载中...")}</td></tr>
              ) : !data?.issues.length ? (
                <tr><td colSpan={9} className="px-4 py-16 text-center text-sm" style={{ color: S.text3 }}>{t("暂无工单")}</td></tr>
              ) : data.issues.map((item, idx) => {
                const isSelected = detailItem?.record_id === item.record_id;
                const rowBg = isSelected ? S.accentBg : (idx % 2 === 0 ? "transparent" : "rgba(0,0,0,0.01)");
                return (
                <tr key={item.record_id}
                  className="cursor-pointer transition-colors"
                  style={{ borderBottom: `1px solid ${S.borderSm}`, background: rowBg, boxShadow: isSelected ? `inset 3px 0 0 ${S.accent}` : "inset 3px 0 0 transparent" }}
                  onClick={() => openDetail(item)}
                  onMouseEnter={(e) => (e.currentTarget.style.background = isSelected ? S.accentBg : S.hover)}
                  onMouseLeave={(e) => (e.currentTarget.style.background = rowBg)}>
                  <td className={tdBase} style={{ width: "56px" }}><PriorityBadge p={item.priority} /></td>
                  <td className="px-3 py-3 max-w-md">
                    <p className="text-sm leading-snug" style={{ color: S.text1, display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>
                      {currentLang === "en" ? stripCategoryPrefix(item.description || "") : item.description}
                    </p>
                    {(item.root_cause_summary || item.root_cause_summary_en) && (
                      <div className="mt-1.5 flex items-start gap-1.5">
                        <span className="mt-px flex-shrink-0 text-[10px] font-semibold" style={{ color: S.accent }}>{t("原因")}</span>
                        <p className="text-xs" style={{ color: S.text2, display: "-webkit-box", WebkitLineClamp: 1, WebkitBoxOrient: "vertical", overflow: "hidden" }}>
                          {lang(item, "root_cause_summary", "root_cause_summary_en")}
                        </p>
                      </div>
                    )}
                    {item.category && (
                      <span className="mt-1 inline-block rounded px-1.5 py-0.5 text-[10px]"
                        style={{ background: S.overlay, color: S.text3 }}>{catShort(item.category || "")}</span>
                    )}
                  </td>
                  <td className={tdBase} style={{ width: "96px" }}>
                    <div className="flex flex-col gap-1">
                      <StatusBadge status={item.local_status} ruleType={item.analysis?.rule_type} />
                      {item.escalated_at && (
                        <span className="inline-flex w-fit rounded-full px-1.5 py-0.5 text-[9px] font-semibold"
                          style={{ background: S.orangeBg, color: S.orange, border: `1px solid ${S.orangeBorder}` }}>
                          {t("已转交")}
                        </span>
                      )}
                      {(item.analysis_count ?? 0) > 1 && (
                        <span className="inline-flex w-fit items-center gap-0.5 rounded-full px-1.5 py-0.5 text-[9px] font-semibold"
                          style={{ background: "rgba(167,139,250,0.12)", color: "#7C3AED", border: "1px solid rgba(167,139,250,0.25)" }}>
                          {t("追问")} ×{(item.analysis_count ?? 0) - 1}
                        </span>
                      )}
                    </div>
                  </td>
                  <td className={tdBase} style={{ width: "64px" }}>
                    <span className="text-xs" style={{ color: S.text2 }}>{item.platform || "—"}</span>
                  </td>
                  <td className={tdBase} style={{ width: "80px" }}><SourceBadge source={item.source} /></td>
                  <td className={tdBase} style={{ width: "96px" }}>
                    {item.created_by ? (
                      <button onClick={(e) => { e.stopPropagation(); updateFilter("created_by", item.created_by!); }}
                        className="text-xs hover:underline" style={{ color: "#2563EB" }}>{item.created_by}</button>
                    ) : <span className="text-xs" style={{ color: S.text3 }}>—</span>}
                  </td>
                  <td className={tdBase} style={{ width: "112px" }}>
                    <span className="font-mono text-xs" style={{ color: S.text3 }}>{formatLocalTime(item.created_at)}</span>
                  </td>
                  <td className={tdBase} style={{ width: "80px" }}>
                    {item.zendesk_id
                      ? <a href={item.zendesk} target="_blank" onClick={(e) => e.stopPropagation()}
                          className="text-xs font-medium hover:underline" style={{ color: "#2563EB" }}>{item.zendesk_id}</a>
                      : <span className="text-xs" style={{ color: S.text3 }}>—</span>}
                  </td>
                  <td className={`${tdBase} text-right`} style={{ width: "144px" }} onClick={(e) => e.stopPropagation()}>
                    <div className="flex items-center justify-end gap-1">
                      {item.local_status === "failed" && (
                        <button onClick={() => handleRetry(item.record_id)}
                          className="rounded-lg px-2.5 py-1 text-[11px] font-semibold"
                          style={{ background: "var(--j-surface)", color: S.accent, border: `1px solid rgba(14,124,134,0.3)` }}>
                          {t("重试")}
                        </button>
                      )}
                      {item.local_status === "failed" && (
                        <button onClick={() => setDeepConfirmId(item.record_id)}
                          className="rounded-lg px-2.5 py-1 text-[11px] font-semibold"
                          style={{ background: "rgba(99,102,241,0.15)", color: "#6366F1", border: "1px solid rgba(99,102,241,0.3)" }}>
                          {t("深度分析")}
                        </button>
                      )}
                      {(item.analysis?.user_reply || item.analysis?.user_reply_en) && (
                        <button onClick={() => copy(lang(item.analysis!, "user_reply", "user_reply_en"))}
                          className="rounded-lg px-2.5 py-1 text-[11px] font-medium"
                          style={{ background: "rgba(34,197,94,0.12)", color: "#16A34A", border: "1px solid rgba(34,197,94,0.25)" }}>
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
                    </div>
                  </td>
                </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <Pagination page={page} totalPages={data?.total_pages ?? 1} onChange={(p) => setPage(p)} />
      </div>

      {/* Detail panel (docked split-view from main) */}
      {detailItem && (
        <aside className="panel-slide-in fixed top-0 right-0 bottom-0 z-30 w-full lg:w-[35%] overflow-y-auto"
          style={{ background: "var(--j-panel)", borderLeft: `1px solid ${S.border}`, boxShadow: "-16px 0 40px -24px rgba(0,0,0,0.25)" }}>
            <div className="sticky top-0 z-10 flex items-center justify-between px-5 py-3"
              style={{ background: "var(--j-panel)", borderBottom: `1px solid ${S.border}` }}>
              <h2 className="text-sm font-semibold" style={{ color: S.text1 }}>{t("工单详情")}</h2>
              <button onClick={closeDetail} title={t("关闭 (Esc)")} aria-label={t("关闭 (Esc)")}
                className="flex items-center gap-1.5 rounded-lg px-2 py-1.5 transition-colors hover:bg-black/5" style={{ color: S.text3 }}>
                <kbd className="text-[10px] font-medium tracking-wide" style={{ color: S.text3 }}>Esc</kbd>
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
            <div className="p-5 space-y-5">
              <section>
                <div className="flex flex-wrap items-center gap-2 mb-3">
                  <PriorityBadge p={detailItem.priority} />
                  <StatusBadge status={detailItem.local_status} ruleType={detailItem.analysis?.rule_type} />
                  {(() => {
                    const la = detailItem.analysis || (issueAnalyses[detailItem.record_id]?.[0]) || null;
                    const deviceType = (la as any)?.device_type || la?.log_metadata?.device_model || "";
                    return deviceType ? (
                      <span className="rounded-full px-2 py-0.5 text-[10px] font-medium"
                        style={{ background: "rgba(99,102,241,0.08)", color: "#6366F1", border: "1px solid rgba(99,102,241,0.15)" }}>
                        {deviceType}
                      </span>
                    ) : null;
                  })()}
                  {detailItem.device_sn && (
                    <span className="rounded-full px-2 py-0.5 text-[10px] font-mono"
                      style={{ background: S.overlay, color: S.text2, border: `1px solid ${S.border}` }}>
                      SN: {detailItem.device_sn}
                    </span>
                  )}
                  <SourceBadge source={detailItem.source} />
                  {detailItem.created_by && (
                    <span className="rounded-full px-2 py-0.5 text-[10px]"
                      style={{ background: S.overlay, color: S.text2 }}>{detailItem.created_by}</span>
                  )}
                  {detailItem.feishu_link && <FeishuLinkBadge href={detailItem.feishu_link} />}
                  {detailItem.zendesk_id && (
                    <a href={detailItem.zendesk} target="_blank"
                      className="inline-flex items-center gap-1 rounded-lg px-2.5 py-1.5 text-xs font-semibold hover:opacity-80"
                      style={{ background: "rgba(0,0,0,0.04)", color: S.text1, border: `1px solid ${S.border}`, textDecoration: "none" }}>
                      Zendesk {detailItem.zendesk_id}
                    </a>
                  )}
                </div>
                {detailItem.category && (
                  <p className="mb-2 text-xs" style={{ color: S.text2 }}>
                    {t("分类")}: <span style={{ color: S.text1 }}>{catShort(detailItem.category || "")}</span>
                  </p>
                )}
                {(() => {
                  const latestAnalysis = detailItem.analysis
                    || (issueAnalyses[detailItem.record_id] && issueAnalyses[detailItem.record_id].length > 0 ? issueAnalyses[detailItem.record_id][0] : null);
                  const lm = latestAnalysis?.log_metadata || {};
                  const deviceType = (latestAnalysis as any)?.device_type || "";

                  const fields = [
                    { l: t("设备型号"), v: lm.device_model || deviceType },
                    { l: t("APP"), v: lm.app_version || detailItem.app_version },
                    { l: t("系统版本"), v: lm.os_version },
                    { l: t("固件"), v: detailItem.firmware },
                    { l: t("用户 UID"), v: lm.uid, mono: true },
                    { l: t("语言/地区"), v: lm.locale },
                    { l: t("API 区域"), v: lm.api_region },
                  ].filter(f => f.v);

                  return (
                    <>
                      <div className="grid grid-cols-2 gap-2 text-xs">
                        {fields.map((f) => (
                          <div key={f.l} className="rounded-lg px-3 py-2" style={{ background: S.overlay }}>
                            <span style={{ color: S.text3 }}>{f.l}</span>
                            <p className={`mt-0.5 font-medium truncate ${f.mono ? "font-mono text-[11px]" : ""}`}
                              style={{ color: S.text1 }} title={f.v || ""}>
                              {f.v || "—"}
                            </p>
                          </div>
                        ))}
                      </div>
                      {lm.file_ids && lm.file_ids.length > 0 && (
                        <div className="mt-2 rounded-lg px-3 py-2 text-xs" style={{ background: S.overlay }}>
                          <span style={{ color: S.text3 }}>{t("关联文件")} ({lm.file_ids.length})</span>
                          <div className="mt-1 flex flex-wrap gap-1">
                            {lm.file_ids.slice(0, 4).map((fid: string) => (
                              <span key={fid} className="rounded px-1.5 py-0.5 font-mono text-[10px]"
                                style={{ background: S.surface, color: S.text2 }}>
                                {fid}
                              </span>
                            ))}
                            {lm.file_ids.length > 4 && (
                              <span className="text-[10px]" style={{ color: S.text3 }}>+{lm.file_ids.length - 4}</span>
                            )}
                          </div>
                        </div>
                      )}
                    </>
                  );
                })()}
              </section>
              <section>
                <h3 className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>{t("问题描述")}</h3>
                <div className="whitespace-pre-wrap rounded-lg p-3 text-sm leading-relaxed" style={{ background: S.overlay, color: S.text2 }}>
                  {currentLang === "en" ? stripCategoryPrefix(detailItem.description || "") : detailItem.description}
                </div>
              </section>
              {/* Attachments / Log Files */}
              {detailItem.log_files && detailItem.log_files.length > 0 && (() => {
                const issueId = detailItem.record_id;
                const images = detailItem.log_files.filter((f: any) => /\.(png|jpe?g|gif|webp|bmp)$/i.test(f.name));
                const logs = detailItem.log_files.filter((f: any) => !/\.(png|jpe?g|gif|webp|bmp)$/i.test(f.name));
                return (
                  <section>
                    <h3 className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>
                      {t("附件")} ({detailItem.log_files.length})
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
                    {logs.length > 0 && (
                      <a href={`/api/local/${issueId}/download-logs`}
                        download
                        className="mt-2 flex items-center justify-center gap-2 w-full rounded-lg py-2 text-xs font-medium transition-colors hover:opacity-80"
                        style={{ background: S.overlay, color: S.accent, border: `1px solid ${S.border}`, textDecoration: "none" }}>
                        <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                          <path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3" />
                        </svg>
                        {t("下载日志")}
                      </a>
                    )}
                  </section>
                );
              })()}

              {detailItem.analysis && (() => {
                const allAnalyses = issueAnalyses[detailItem.record_id];
                const analyses = allAnalyses && allAnalyses.length > 0 ? allAnalyses : [detailItem.analysis];
                return (
                  <AnalysisResultView
                    analyses={analyses}
                    issueId={detailItem.record_id}
                    lang={currentLang}
                    collapsedEvidence={collapsedEvidence}
                    setCollapsedEvidence={setCollapsedEvidence}
                    activeTask={activeTasks[detailItem.record_id]}
                    followupText={followupText}
                    setFollowupText={setFollowupText}
                    followupSubmitting={followupSubmitting}
                    onStartFollowup={startFollowup}
                    onDeepAnalysis={handleDeepAnalysis}
                    onCopy={copy}
                  />
                );
              })()}

              {detailItem.task?.error && (
                <section>
                  <h3 className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>{t("失败原因")}</h3>
                  <div className="rounded-lg p-3 text-sm" style={{ background: "rgba(239,68,68,0.08)", color: "#DC2626", border: "1px solid rgba(239,68,68,0.2)" }}>
                    {detailItem.task.error}
                  </div>
                </section>
              )}
              {/* Escalation info — share_link prefers in-session state, falls back to persisted DB value */}
              {(() => {
                const persistedLink = detailItem.escalation_share_link || "";
                const link = escalateLinks[detailItem.record_id] || persistedLink;
                if (!detailItem.escalated_at && !link) return null;
                return (
                  <section className="rounded-lg p-3 space-y-2" style={{ background: S.orangeBg, border: `1px solid ${S.orangeBorder}` }}>
                    <div className="flex items-center gap-2 mb-1">
                      <span className="inline-flex rounded-full px-2 py-0.5 text-[10px] font-medium"
                        style={{ background: S.orangeBg, color: S.orange, border: `1px solid ${S.orangeBorder}` }}>
                        {t("已转交")}
                      </span>
                      {detailItem.escalated_by && (
                        <span className="text-xs" style={{ color: S.text2 }}>{t("转交人")}: {detailItem.escalated_by}</span>
                      )}
                      {detailItem.escalated_at && (
                        <span className="text-[10px] ml-auto" style={{ color: S.text3 }}>{formatLocalTime(detailItem.escalated_at)}</span>
                      )}
                    </div>
                    {detailItem.escalation_note && (
                      <p className="text-xs mt-1" style={{ color: S.orange }}>{t("转交备注")}: {detailItem.escalation_note}</p>
                    )}
                    {link && (
                      <a href={link} target="_blank"
                        className="flex items-center justify-center gap-2 w-full rounded-lg py-2 text-sm font-semibold transition-colors hover:opacity-80"
                        style={{ background: S.orange, color: "#FFFFFF", textDecoration: "none" }}>
                        <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                          <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 6H5.25A2.25 2.25 0 003 8.25v10.5A2.25 2.25 0 005.25 21h10.5A2.25 2.25 0 0018 18.75V10.5m-10.5 6L21 3m0 0h-5.25M21 3v5.25" />
                        </svg>
                        {t("打开飞书群")}
                      </a>
                    )}
                  </section>
                );
              })()}
              <section className="pt-4 space-y-2" style={{ borderTop: `1px solid ${S.border}` }}>
                {/* Mark complete — for done/failed, syncs to Feishu */}
                {(detailItem.local_status === "done" || detailItem.local_status === "failed") && (
                  <button onClick={() => { setCompleteReason(""); setCompleteId(detailItem.record_id); }}
                    className="w-full rounded-lg py-2.5 text-sm font-semibold flex items-center justify-center gap-2"
                    style={{ background: "rgba(34,197,94,0.12)", color: "#16A34A", border: "1px solid rgba(34,197,94,0.25)" }}>
                    <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                    </svg>
                    {t("标记完成")}
                  </button>
                )}
                {/* Already escalated → show "join group" CTA in the same slot as the escalate button */}
                {(detailItem.local_status === "done" || detailItem.local_status === "failed") && detailItem.escalated_at && (() => {
                  const link = escalateLinks[detailItem.record_id] || detailItem.escalation_share_link || "";
                  if (!link) return null;
                  return (
                    <a href={link} target="_blank"
                      className="w-full rounded-lg py-2.5 text-sm font-semibold flex items-center justify-center gap-2"
                      style={{ background: S.orange, color: "#FFFFFF", textDecoration: "none" }}>
                      <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M18 9v3m0 0v3m0-3h3m-3 0h-3m-2-5a4 4 0 11-8 0 4 4 0 018 0zM3 20a6 6 0 0112 0v1H3v-1z" />
                      </svg>
                      {t("加入飞书群")}
                    </a>
                  );
                })()}
                {/* Escalate button — show for done/failed (not already escalated) */}
                {(detailItem.local_status === "done" || detailItem.local_status === "failed") && !detailItem.escalated_at && (
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
                        <button onClick={() => handleEscalate(detailItem.record_id)}
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
                {detailItem.local_status === "failed" && (
                  <div className="flex gap-2">
                    <button onClick={() => { handleRetry(detailItem.record_id); closeDetail(); }}
                      className="flex-1 rounded-lg py-2.5 text-sm font-semibold"
                      style={{ background: S.accent, color: "#FFFFFF" }}>
                      {t("重新分析")}
                    </button>
                    <button onClick={() => setDeepConfirmId(detailItem.record_id)}
                      className="flex-1 rounded-lg py-2.5 text-sm font-semibold"
                      style={{ background: "rgba(99,102,241,0.15)", color: "#6366F1", border: "1px solid rgba(99,102,241,0.3)" }}>
                      {t("深度分析")}
                    </button>
                  </div>
                )}
                {detailItem.local_status === "done" && (
                  <div className="space-y-2">
                    <button onClick={() => { handleMarkInaccurate(detailItem.record_id); closeDetail(); }}
                      className="w-full rounded-lg py-2.5 text-sm font-medium"
                      style={{ background: "rgba(239,68,68,0.10)", color: "#DC2626", border: "1px solid rgba(239,68,68,0.25)" }}>
                      {t("标记为不准确")}
                    </button>
                  </div>
                )}
                {/* Transfer to Feishu — with intercept dialog */}
                {(detailItem.local_status === "done" || detailItem.local_status === "failed") && (
                  showFeishuTransferDialog ? (
                    <div className="rounded-lg p-4 space-y-3" style={{ background: "rgba(96,165,250,0.06)", border: "1px solid rgba(96,165,250,0.2)" }}>
                      <div className="flex items-start gap-2">
                        <svg className="h-4 w-4 flex-shrink-0 mt-0.5" fill="none" viewBox="0 0 24 24" stroke="#2563EB" strokeWidth={2}>
                          <path strokeLinecap="round" strokeLinejoin="round" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                        </svg>
                        <div>
                          <p className="text-sm font-medium" style={{ color: "#1D4ED8" }}>{t("建议使用群聊跟进")}</p>
                          <p className="mt-1 text-xs" style={{ color: "#5B6470" }}>
                            {t("飞书工单即将停用，建议直接点击「转交工程师」创建群聊跟进，更高效便捷。")}
                          </p>
                        </div>
                      </div>
                      <div className="flex gap-2">
                        <button
                          onClick={() => { setShowFeishuTransferDialog(false); setShowEscalateDialog(true); }}
                          className="flex-1 rounded-lg py-2 text-sm font-semibold"
                          style={{ background: S.orangeBg, color: S.orange, border: `1px solid ${S.orangeBorder}` }}>
                          {t("转交工程师（推荐）")}
                        </button>
                        <button
                          onClick={() => {
                            setShowFeishuTransferDialog(false);
                            const base = "https://nicebuild.feishu.cn/share/base/form/shrcnGuYEnRrbbVw4Y6evkyUDCo";
                            const params = new URLSearchParams();
                            const appUrl = `${window.location.origin}/tracking?detail=${detailItem.record_id}`;
                            const desc = `Apollo 工单: ${appUrl}\n\n${detailItem.description || ""}`;
                            params.set("prefill_问题描述", desc);
                            if (detailItem.zendesk) params.set("prefill_Zendesk 工单链接", detailItem.zendesk);
                            if (detailItem.feishu_link) params.set("prefill_飞书工单链接", detailItem.feishu_link);
                            const latestAnalysis = issueAnalyses[detailItem.record_id]?.[0] || detailItem.analysis;
                            if (latestAnalysis?.root_cause) params.set("prefill_处理结果", latestAnalysis.root_cause);
                            if (detailItem.root_cause_summary) params.set("prefill_一句话归因", detailItem.root_cause_summary);
                            window.open(`${base}?${params.toString()}`, "_blank");
                          }}
                          className="rounded-lg px-4 py-2 text-xs font-medium"
                          style={{ border: `1px solid ${S.border}`, color: S.text3 }}>
                          {t("仍然创建飞书工单")}
                        </button>
                      </div>
                    </div>
                  ) : (
                    <button onClick={() => setShowFeishuTransferDialog(true)}
                      className="w-full rounded-lg py-2.5 text-sm font-semibold flex items-center justify-center gap-2"
                      style={{ background: "rgba(96,165,250,0.12)", color: "#2563EB", border: "1px solid rgba(96,165,250,0.25)" }}>
                      <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 6H5.25A2.25 2.25 0 003 8.25v10.5A2.25 2.25 0 005.25 21h10.5A2.25 2.25 0 0018 18.75V10.5m-10.5 6L21 3m0 0h-5.25M21 3v5.25" />
                      </svg>
                      {t("转飞书工单")}
                    </button>
                  )
                )}
              </section>
            </div>
        </aside>
      )}

      {/* Deep-analysis confirmation dialog */}
      {deepConfirmId && (
        <div className="j-fade fixed inset-0 z-[60] flex items-center justify-center p-4" style={{ background: "rgba(0,0,0,0.55)" }} onClick={() => setDeepConfirmId(null)}>
          <div className="j-pop w-full max-w-md rounded-xl p-5" style={{ background: "var(--j-panel)", border: `1px solid ${S.border}` }} onClick={(e) => e.stopPropagation()}>
            <div className="flex items-start gap-3">
              <div className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-lg" style={{ background: "rgba(99,102,241,0.12)" }}>
                <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="#6366F1" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
              </div>
              <div className="flex-1">
                <h3 className="text-sm font-semibold" style={{ color: S.text1 }}>{t("确认开始深度分析")}</h3>
                <p className="mt-2 text-xs leading-relaxed" style={{ color: S.text2 }}>{t("深度分析风险提示")}</p>
              </div>
            </div>
            <div className="mt-5 flex justify-end gap-2">
              <button onClick={() => setDeepConfirmId(null)}
                className="rounded-lg px-4 py-2 text-sm font-medium"
                style={{ border: `1px solid ${S.border}`, color: S.text2 }}>
                {t("取消")}
              </button>
              <button onClick={confirmDeepAnalysis}
                className="rounded-lg px-4 py-2 text-sm font-semibold"
                style={{ background: "#6366F1", color: "#FFFFFF" }}>
                {t("确定开始")}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Mark-complete reason dialog — reason is required (功能 3) */}
      {completeId && (
        <div className="j-fade fixed inset-0 z-[60] flex items-center justify-center p-4" style={{ background: "rgba(0,0,0,0.55)" }} onClick={() => setCompleteId(null)}>
          <div className="j-pop w-full max-w-md rounded-xl p-5" style={{ background: "var(--j-panel)", border: `1px solid ${S.border}` }} onClick={(e) => e.stopPropagation()}>
            <div className="flex items-start gap-3">
              <div className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-lg" style={{ background: "rgba(34,197,94,0.12)" }}>
                <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="#16A34A" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
              </div>
              <div className="flex-1">
                <h3 className="text-sm font-semibold" style={{ color: S.text1 }}>{t("标记完成")}</h3>
                <p className="mt-1 text-xs" style={{ color: S.text2 }}>{t("请输入标记完成的原因")}</p>
              </div>
            </div>
            <div className="mt-4">
              <label className="mb-1.5 block text-[10px] font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>
                {t("标记完成原因")}
              </label>
              <textarea
                value={completeReason}
                onChange={(e) => setCompleteReason(e.target.value)}
                placeholder={t("输入标记完成的原因…")}
                rows={3}
                autoFocus
                className="w-full resize-none rounded-lg px-3 py-2 text-sm outline-none"
                style={{ background: S.overlay, border: `1px solid ${S.borderSm}`, color: S.text1 }}
              />
            </div>
            <div className="mt-5 flex justify-end gap-2">
              <button onClick={() => setCompleteId(null)}
                className="rounded-lg px-4 py-2 text-sm font-medium"
                style={{ border: `1px solid ${S.border}`, color: S.text2 }}>
                {t("取消")}
              </button>
              <button
                onClick={() => {
                  const reason = completeReason.trim();
                  if (!reason) { setToast(t("请填写原因")); return; }
                  const id = completeId;
                  setCompleteId(null);
                  handleMarkComplete(id, reason);
                }}
                disabled={!completeReason.trim()}
                className="rounded-lg px-4 py-2 text-sm font-semibold disabled:opacity-40"
                style={{ background: "#16A34A", color: "#FFFFFF" }}>
                {t("确定")}
              </button>
            </div>
          </div>
        </div>
      )}

      {toast && <Toast msg={toast} onClose={() => setToast("")} />}
    </div>
  );
}
