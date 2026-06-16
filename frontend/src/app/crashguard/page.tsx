"use client";

import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams, usePathname } from "next/navigation";
import { useT } from "@/lib/i18n";
import { Toast } from "@/components/Toast";
import MarkdownText from "@/components/MarkdownText";
import {
  fetchCrashTop,
  fetchCrashIssue,
  fetchCrashHealth,
  updateCrashIssue,
  analyzeCrashIssue,
  batchAnalyzeCrash,
  fetchCrashAnalyses,
  followupCrashIssue,
  fetchCrashAnalysisStatus,
  runCrashDailyReport,
  approveCrashPr,
  fetchAutoPrQueue,
  fetchCrashLatestRelease,
  fetchCrashVersionDistribution,
  fetchCrashOsVersionDistribution,
  fetchCrashPlatformSummary,
  triggerCrashWarmup,
  startDeepAnalysis,
  fetchDiagnosisStatus,
  confirmDiagnosisHypothesis,
  type DiagnosisStatus,
  type DiagnosisHypothesis,
  type AutoPrQueueResponse,
  type CrashAnalysisRecord,
  type CrashTopItem,
  type CrashTopAggregates,
  type CrashIssueDetail,
  type CrashSortBy,
  type CrashStatus,
  type CrashVersionSlice,
  type CrashOsVersionSlice,
  type CrashPlatformSummary,
} from "@/lib/api";
import { getBatchTopN } from "@/lib/crashguard-prefs";

// jarvis 主站浅色金调（Firebase-style 布局 + 主题对齐）
const D = {
  bg: "#F1F4F3",
  surface: "#FFFFFF",
  surfaceAlt: "#F1F4F3",
  border: "rgba(0,0,0,0.08)",
  borderStrong: "rgba(0,0,0,0.14)",
  text1: "#15181E",
  text2: "#5B6470",
  text3: "#9CA3AF",
  accent: "#0E7C86",                       // jarvis gold
  accentBg: "rgba(14,124,134,0.08)",
  ok: "#16A34A",
  warn: "#D97706",
  warnBg: "rgba(217,119,6,0.10)",
  danger: "#DC2626",
  dangerBg: "rgba(220,38,38,0.08)",
  p0: "#DC2626",
  p1: "#2563EB",
  hover: "#E8ECEA",
};

const STATUS_OPTIONS: { value: CrashStatus; label: string }[] = [
  { value: "open", label: "未处理" },
  { value: "investigating", label: "排查中" },
  { value: "resolved_by_pr", label: "已修复" },
  { value: "ignored", label: "忽略" },
  { value: "wontfix", label: "暂不修" },
];

const STATUS_COLORS: Record<CrashStatus, { fg: string; bg: string }> = {
  open: { fg: "#DC2626", bg: "rgba(220,38,38,0.08)" },
  investigating: { fg: "#D97706", bg: "rgba(217,119,6,0.10)" },
  resolved_by_pr: { fg: "#16A34A", bg: "rgba(22,163,74,0.10)" },
  ignored: { fg: "#5B6470", bg: "rgba(107,114,128,0.10)" },
  wontfix: { fg: "#5B6470", bg: "rgba(107,114,128,0.10)" },
};

const PLATFORM_ALIASES: Record<string, string> = {
  flutter: "Flutter",
  ios: "iOS",
  android: "Android",
  browser: "Web",
};

function platformLabel(p: string): string {
  const k = (p || "").toLowerCase();
  return PLATFORM_ALIASES[k] || p || "—";
}

function compactNumber(n: number): string {
  if (n < 1000) return n.toString();
  if (n < 1_000_000) return (n / 1000).toFixed(n >= 10_000 ? 0 : 1) + "K";
  return (n / 1_000_000).toFixed(1) + "M";
}

function tierColor(tier: string) {
  return tier === "P0" ? D.p0 : D.p1;
}

function versionRange(a: string, b: string): string {
  if (!a && !b) return "—";
  if (a && !b) return a;
  if (!a && b) return b;
  if (a === b) return a;
  return `${a} – ${b}`;
}

function timeAgo(iso?: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const diff = Date.now() - d.getTime();
  const mins = Math.floor(diff / 60_000);
  if (mins < 60) return `${mins} 分钟前`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs} 小时前`;
  const days = Math.floor(hrs / 24);
  return `${days} 天前`;
}

export default function CrashguardPage() {
  return (
    <Suspense fallback={<div style={{ padding: 32, color: "#5B6470" }}>加载中...</div>}>
      <CrashguardPageInner />
    </Suspense>
  );
}

function CrashguardPageInner() {
  const t = useT();
  const [items, setItems] = useState<CrashTopItem[]>([]);
  const [date, setDate] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [batching, setBatching] = useState(false);
  const [reportBusy, setReportBusy] = useState(false);
  const [reportLoading, setReportLoading] = useState<{
    type: "morning" | "evening";
    startedAt: number;
  } | null>(null);
  const [reportElapsed, setReportElapsed] = useState(0);
  const [reportModal, setReportModal] = useState<{
    title: string;
    preview: string;
    reportType: "morning" | "evening";
    error?: string;
  } | null>(null);
  // 分页 + 后端过滤（全部经 URL query 同步，支持深链）
  const [aggregates, setAggregates] = useState<CrashTopAggregates | null>(null);
  // 全量 aggregates（不受 fatality filter 影响），用于 KPI 顶栏始终展示完整 fatal/non-fatal 总量
  const [globalAggregates, setGlobalAggregates] = useState<CrashTopAggregates | null>(null);
  const [totalCount, setTotalCount] = useState(0);
  const [totalPages, setTotalPages] = useState(1);
  const PAGE_SIZE = 40;
  // tier filter 留在客户端（UI 显示用；当前页结果上的二次过滤）
  const [tierFilter, setTierFilter] = useState<"all" | "P0" | "P1">("all");
  const [autoPrQueue, setAutoPrQueue] = useState<AutoPrQueueResponse | null>(null);
  const [latestRelease, setLatestRelease] = useState<{ flutter: string; android: string; ios: string } | null>(null);
  const [latestReleaseSource, setLatestReleaseSource] = useState<{ flutter: string; android: string; ios: string } | null>(null);
  // 用户量最大版本（仅 android / ios，24h Datadog RUM 派生 / crash_issues fallback）
  const [topUserVersion, setTopUserVersion] = useState<
    Partial<Record<"android" | "ios", { version: string; users: number }>> | null
  >(null);
  const [topUserVersionSource, setTopUserVersionSource] = useState<
    Partial<Record<"android" | "ios", string>> | null
  >(null);
  const [versionDistribution, setVersionDistribution] = useState<
    Partial<Record<"android" | "ios", CrashVersionSlice[]>>
  >({});
  const [osVersionDistribution, setOsVersionDistribution] = useState<
    Partial<Record<"android" | "ios", CrashOsVersionSlice[]>>
  >({});
  const [platformSummary, setPlatformSummary] = useState<
    Partial<Record<"android" | "ios", CrashPlatformSummary>>
  >({});
  // 首次进入若空数据 → 自动 bootstrap（拉数 + AI 分析），避免用户面对空白
  const [bootstrapping, setBootstrapping] = useState(false);
  const [bootstrapElapsed, setBootstrapElapsed] = useState(0);
  const [bootstrapDone, setBootstrapDone] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  // URL → state（深链入口；与 reports 页相同思路）
  const parsePlatform = (v: string | null): string =>
    ["all", "android", "ios", "flutter", "browser"].includes(v || "") ? (v as string) : "all";
  const parseFatality = (v: string | null): "all" | "fatal" | "non_fatal" =>
    v === "all" || v === "non_fatal" ? v : "fatal"; // 默认 fatal
  const parseStatus = (v: string | null): "all" | CrashStatus =>
    ["all", "open", "investigating", "resolved_by_pr", "ignored", "wontfix"].includes(v || "")
      ? (v as "all" | CrashStatus)
      : "all";
  const parseSort = (v: string | null): CrashSortBy =>
    v === "impact" || v === "users" || v === "new_first" ? v : "events"; // 默认 events
  const parsePageNum = (v: string | null): number => {
    const n = parseInt(v || "", 10);
    return Number.isFinite(n) && n > 0 ? n : 1;
  };
  // 时间窗口：24/168/336/720 小时 = 1d/7d/14d/30d；默认 24h 与 Datadog 首页对齐
  const parseWindow = (v: string | null): 24 | 168 | 336 | 720 => {
    const n = parseInt(v || "", 10);
    return n === 168 || n === 336 || n === 720 ? (n as 168 | 336 | 720) : 24;
  };

  const platformFilter = parsePlatform(searchParams?.get("platform") || null);
  const fatalityFilter = parseFatality(searchParams?.get("fatality") || null);
  const statusFilter = parseStatus(searchParams?.get("status") || null);
  const sortBy = parseSort(searchParams?.get("sort") || null);
  const page = parsePageNum(searchParams?.get("page") || null);
  const windowHours = parseWindow(searchParams?.get("win") || null);
  // search 不走 URL 立即同步——避免每个字符都 push history；用 debounced 内部 state
  const [search, setSearch] = useState<string>(searchParams?.get("search") || "");
  const debouncedSearchRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // URL ↔ 选中态双向同步：?issue=<id> deep link
  const syncSelectedToUrl = (issueId: string | null) => {
    const params = new URLSearchParams(Array.from(searchParams?.entries() || []));
    if (issueId) {
      params.set("issue", issueId);
    } else {
      params.delete("issue");
    }
    const qs = params.toString();
    router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
  };

  // 列表 filter / sort / page / search 写 URL（router.replace 不堆历史栈）
  const updateListQuery = useCallback(
    (
      patch: Partial<{
        platform: string;
        fatality: "all" | "fatal" | "non_fatal";
        status: "all" | CrashStatus;
        sort: CrashSortBy;
        page: number;
        search: string;
        win: 24 | 168 | 336 | 720;
      }>,
    ) => {
      const params = new URLSearchParams(Array.from(searchParams?.entries() || []));
      const setOrDel = (key: string, val: string | undefined, isDefault: boolean) => {
        if (val === undefined) return;
        if (isDefault) params.delete(key);
        else params.set(key, val);
      };
      if ("platform" in patch) setOrDel("platform", patch.platform, patch.platform === "all" || !patch.platform);
      if ("fatality" in patch) setOrDel("fatality", patch.fatality, patch.fatality === "fatal" || !patch.fatality);
      if ("status" in patch) setOrDel("status", patch.status, patch.status === "all" || !patch.status);
      if ("sort" in patch) setOrDel("sort", patch.sort, patch.sort === "events" || !patch.sort);
      if ("page" in patch) setOrDel("page", patch.page ? String(patch.page) : undefined, patch.page === 1 || !patch.page);
      if ("search" in patch) setOrDel("search", patch.search, !patch.search);
      if ("win" in patch) setOrDel("win", patch.win ? String(patch.win) : undefined, patch.win === 24 || !patch.win);
      const qs = params.toString();
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    },
    [router, pathname, searchParams],
  );

  const setPlatformFilter = (v: string) => updateListQuery({ platform: v, page: 1 });
  const setFatalityFilter = (v: "all" | "fatal" | "non_fatal") => updateListQuery({ fatality: v, page: 1 });
  const setStatusFilter = (v: "all" | CrashStatus) => updateListQuery({ status: v, page: 1 });
  const setSortBy = (v: CrashSortBy) => updateListQuery({ sort: v, page: 1 });
  const setPage = (v: number) => updateListQuery({ page: v });
  const setWindowHours = (v: 24 | 168 | 336 | 720) => updateListQuery({ win: v, page: 1 });

  // search 输入 debounce 300ms → push 到 URL
  useEffect(() => {
    if (debouncedSearchRef.current) clearTimeout(debouncedSearchRef.current);
    debouncedSearchRef.current = setTimeout(() => {
      const cur = searchParams?.get("search") || "";
      if (search !== cur) {
        updateListQuery({ search, page: 1 });
      }
    }, 300);
    return () => {
      if (debouncedSearchRef.current) clearTimeout(debouncedSearchRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search]);
  const [detail, setDetail] = useState<CrashIssueDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [analyses, setAnalyses] = useState<CrashAnalysisRecord[]>([]);
  const [followupText, setFollowupText] = useState("");
  const [followupSubmitting, setFollowupSubmitting] = useState(false);
  const [toast, setToast] = useState<{ msg: string; type: "success" | "error" } | null>(null);
  const [datadogConfigured, setDatadogConfigured] = useState<boolean | null>(null);
  const [savingPatch, setSavingPatch] = useState(false);
  const [analyzing, setAnalyzing] = useState(false);
  // 重新分析 modal：允许用户输入引导 prompt
  const [reanalyzeModal, setReanalyzeModal] = useState<{ issueId: string } | null>(null);
  const [reanalyzePrompt, setReanalyzePrompt] = useState("");
  // 自动化修复 PR 创建状态：按 analysis_id 跟踪 loading
  const [creatingPr, setCreatingPr] = useState<number | null>(null);
  // Phase 1 深度诊断
  const [diagRunId, setDiagRunId] = useState<string | null>(null);
  const [diagStatus, setDiagStatus] = useState<DiagnosisStatus | null>(null);
  const [diagLoading, setDiagLoading] = useState(false);
  const [diagConfirming, setDiagConfirming] = useState<string | null>(null);

  const platforms = useMemo(() => {
    const set = new Set<string>();
    items.forEach((i) => set.add((i.platform || "").toLowerCase()));
    return Array.from(set).filter(Boolean).sort();
  }, [items]);

  // 客户端二次过滤：仅 tier（保留 UI 即时性，不影响 backend 分页结果总数）
  const filteredItems = useMemo(() => {
    if (tierFilter === "all") return items;
    return items.filter((i) => i.tier === tierFilter);
  }, [items, tierFilter]);

  // 头部统计：优先用后端 aggregates；本地回落保兜底（loading 期间 UI 不抽）
  const totals = useMemo(() => {
    if (aggregates) {
      const ag = aggregates as {
        // user 维度（2026-05-21 加，主指标）
        crash_free_users_pct?: number | null;
        crash_free_total_users?: number;
        crash_free_crashed_users?: number;
        fatal_events?: number;
        non_fatal_events?: number;
      } & typeof aggregates;
      return {
        events: aggregates.total_events,
        sessions: aggregates.total_sessions,
        p0: aggregates.p0_count,
        surge: aggregates.surge_count,
        fatalEvents: ag.fatal_events ?? 0,
        nonFatalEvents: ag.non_fatal_events ?? 0,
        fatalCount: aggregates.fatal_count,
        nonFatalCount: aggregates.non_fatal_count,
        // User 维度（主）
        crashFreeUsersPct: ag.crash_free_users_pct ?? null,
        crashFreeTotalUsers: ag.crash_free_total_users ?? 0,
        crashFreeCrashedUsers: ag.crash_free_crashed_users ?? 0,
        // Session 维度（FYI 副指标）
        crashFreeSessionsPct: aggregates.crash_free_sessions_pct ?? null,
        crashFreeTotalSessions: aggregates.crash_free_total_sessions ?? 0,
      };
    }
    const events = items.reduce((s, i) => s + (i.events_count || 0), 0);
    const sessions = items.reduce((s, i) => s + (i.sessions_affected || 0), 0);
    const p0 = items.filter((i) => i.tier === "P0").length;
    const surge = items.filter((i) => i.is_surge).length;
    const fatalItems = items.filter((i) => (i.fatality || "fatal") === "fatal");
    const nonFatalItems = items.filter((i) => i.fatality === "non_fatal");
    return {
      events, sessions, p0, surge,
      fatalEvents: fatalItems.reduce((s, i) => s + (i.events_count || 0), 0),
      nonFatalEvents: nonFatalItems.reduce((s, i) => s + (i.events_count || 0), 0),
      fatalCount: fatalItems.length, nonFatalCount: nonFatalItems.length,
      crashFreeUsersPct: null as number | null,
      crashFreeTotalUsers: 0,
      crashFreeCrashedUsers: 0,
      crashFreeSessionsPct: null as number | null,
      crashFreeTotalSessions: 0,
    };
  }, [items, aggregates]);

  const loadTop = useCallback(async () => {
    setLoading(true);
    try {
      // 主查询：受当前 filters 影响（用于表格 + 部分聚合）
      // 全量查询（仅当 fatalityFilter 在过滤时）：fatality="" 拉一份用于 KPI 顶栏始终展示全量
      const mainReq = fetchCrashTop(PAGE_SIZE, undefined, {
        page,
        page_size: PAGE_SIZE,
        fatality: fatalityFilter === "all" ? "" : fatalityFilter,
        platform: platformFilter === "all" ? "" : platformFilter,
        status: statusFilter === "all" ? "" : statusFilter,
        search: searchParams?.get("search") || "",
        sort_by: sortBy,
        kinds: "all",
        window_hours: windowHours,
      });
      const globalReq = fatalityFilter === "all"
        ? Promise.resolve(null)
        : fetchCrashTop(1, undefined, {
            page: 1,
            page_size: 1,
            fatality: "",
            platform: platformFilter === "all" ? "" : platformFilter,
            status: statusFilter === "all" ? "" : statusFilter,
            search: searchParams?.get("search") || "",
            sort_by: sortBy,
            kinds: "all",
            window_hours: windowHours,
          });
      const [resp, h, globalResp] = await Promise.all([mainReq, fetchCrashHealth(), globalReq]);
      setItems(resp.issues);
      setDate(resp.date);
      setAggregates(resp.aggregates || null);
      setGlobalAggregates(globalResp?.aggregates || resp.aggregates || null);
      setTotalCount(resp.total ?? resp.issues.length);
      setTotalPages(resp.total_pages || 1);
      setDatadogConfigured(h.datadog_configured);
    } catch (e: any) {
      setToast({ msg: e.message || "load failed", type: "error" });
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, fatalityFilter, platformFilter, statusFilter, sortBy, windowHours, searchParams]);

  const loadDetail = async (issueId: string) => {
    setDetailLoading(true);
    setSelectedId(issueId);
    setDetail(null);
    setAnalyses([]);
    setFollowupText("");
    setDiagRunId(null);
    setDiagStatus(null);
    setDiagLoading(false);
    setDiagConfirming(null);
    syncSelectedToUrl(issueId);
    try {
      const [d, list] = await Promise.all([
        fetchCrashIssue(issueId, undefined, windowHours),
        fetchCrashAnalyses(issueId).catch(() => ({ analyses: [] as CrashAnalysisRecord[] })),
      ]);
      setDetail(d);
      setAnalyses((list as any).analyses || []);
    } catch (e: any) {
      setToast({ msg: e.message || "detail failed", type: "error" });
    } finally {
      setDetailLoading(false);
    }
  };

  const onPatch = async (issueId: string, patch: { status?: CrashStatus; assignee?: string }) => {
    setSavingPatch(true);
    try {
      const res = await updateCrashIssue(issueId, patch);
      setItems((prev) =>
        prev.map((it) =>
          it.datadog_issue_id === issueId ? { ...it, status: res.status, assignee: res.assignee } : it,
        ),
      );
      if (detail && detail.datadog_issue_id === issueId) {
        setDetail({ ...detail, status: res.status, assignee: res.assignee });
      }
    } catch (e: any) {
      setToast({ msg: e.message || "save failed", type: "error" });
    } finally {
      setSavingPatch(false);
    }
  };

  const onAnalyze = async (issueId: string, userPrompt = "") => {
    if (analyzing) return;
    setAnalyzing(true);
    setToast({
      msg: userPrompt
        ? t("AI 分析中（带引导 prompt），可能需要 30-90 秒...")
        : t("AI 分析中，可能需要 30-90 秒..."),
      type: "success",
    });
    try {
      const res = await analyzeCrashIssue(issueId, userPrompt);
      if (res.status === "failed") {
        setToast({ msg: t("分析失败: ") + (res.error || "unknown"), type: "error" });
      } else {
        setToast({ msg: t("分析完成"), type: "success" });
      }
      if (selectedId === issueId) {
        const [fresh, list] = await Promise.all([
          fetchCrashIssue(issueId, undefined, windowHours),
          fetchCrashAnalyses(issueId).catch(() => ({ analyses: [] as CrashAnalysisRecord[] })),
        ]);
        setDetail(fresh);
        setAnalyses((list as any).analyses || []);
      }
    } catch (e: any) {
      setToast({ msg: e.message || "analyze failed", type: "error" });
    } finally {
      setAnalyzing(false);
    }
  };

  const submitReanalyze = async () => {
    if (!reanalyzeModal) return;
    const issueId = reanalyzeModal.issueId;
    const prompt = reanalyzePrompt.trim();
    setReanalyzeModal(null);
    setReanalyzePrompt("");
    await onAnalyze(issueId, prompt);
  };

  const onCreatePr = async (analysisId: number, issueId: string) => {
    if (creatingPr !== null) return;
    setCreatingPr(analysisId);
    setToast({ msg: t("正在创建修复 PR，请稍候..."), type: "success" });
    try {
      const res = await approveCrashPr(analysisId);
      const prUrls = res.pr_url
        ? [res.pr_url]
        : (res.prs || []).filter((p) => p.ok && p.pr_url).map((p) => p.pr_url as string);
      if (res.ok && prUrls.length > 0) {
        const count = prUrls.length;
        setToast({ msg: count > 1 ? `${t("修复 PR 已创建")} x${count}` : t("修复 PR 已创建"), type: "success" });
        if (selectedId === issueId) {
          const fresh = await fetchCrashIssue(issueId, undefined, windowHours);
          setDetail(fresh);
        }
        // 顺便刷新列表（让行内 has_pr / pr_url 同步）
        await loadTop();
      } else {
        const firstErr = (res.prs || []).find((p) => !p.ok)?.error;
        setToast({ msg: t("创建失败: ") + (res.error || res.reason || firstErr || "unknown"), type: "error" });
      }
    } catch (e: any) {
      setToast({ msg: e.message || "create PR failed", type: "error" });
    } finally {
      setCreatingPr(null);
    }
  };

  const onStartDeepAnalysis = async (issueId: string) => {
    setDiagLoading(true);
    setDiagStatus(null);
    try {
      const { run_id } = await startDeepAnalysis(issueId);
      setDiagRunId(run_id);
      const poll = async () => {
        try {
          const st = await fetchDiagnosisStatus(run_id);
          setDiagStatus(st as DiagnosisStatus);
          if (st.status === "pending" || st.status === "running") {
            setTimeout(poll, 8000);
          }
        } catch {
          // ignore poll error
        }
      };
      setTimeout(poll, 3000);
    } catch (e: any) {
      setToast({ msg: e.message || "deep analysis failed", type: "error" });
    } finally {
      setDiagLoading(false);
    }
  };

  const onConfirmHypothesis = async (runId: string, hypothesisId: string, issueId: string) => {
    setDiagConfirming(hypothesisId);
    try {
      const { phase2_run_id } = await confirmDiagnosisHypothesis(runId, hypothesisId);
      setToast({ msg: `Phase 2 已触发，run_id: ${phase2_run_id.slice(0, 8)}`, type: "success" });
      const list = await fetchCrashAnalyses(issueId).catch(() => ({ analyses: [] }));
      setAnalyses((list as any).analyses || []);
    } catch (e: any) {
      setToast({ msg: e.message || "confirm failed", type: "error" });
    } finally {
      setDiagConfirming(null);
    }
  };

  const refreshAnalyses = async (issueId: string) => {
    try {
      const list = await fetchCrashAnalyses(issueId);
      setAnalyses(list.analyses || []);
    } catch {}
  };

  const startFollowup = async (issueId: string, question: string) => {
    if (!question.trim() || followupSubmitting) return;
    setFollowupSubmitting(true);
    try {
      const { run_id } = await followupCrashIssue(issueId, question.trim());
      setFollowupText("");
      setToast({ msg: t("追问已提交，等待 AI 回答..."), type: "success" });
      // 立即刷一次（带 pending 行进入会话流）
      await refreshAnalyses(issueId);
      // 轮询直到完成
      const deadline = Date.now() + 8 * 60 * 1000;
      let delay = 3000;
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, delay));
        delay = Math.min(delay + 1000, 8000);
        const st = await fetchCrashAnalysisStatus(run_id);
        if (st.status === "success" || st.status === "failed" || st.status === "empty") {
          await refreshAnalyses(issueId);
          setToast({
            msg: st.status === "success" ? t("追问已回答") : t("追问失败：") + (st.error || st.status),
            type: st.status === "success" ? "success" : "error",
          });
          return;
        }
        await refreshAnalyses(issueId);
      }
    } catch (e: any) {
      setToast({ msg: e.message || "followup failed", type: "error" });
    } finally {
      setFollowupSubmitting(false);
    }
  };

  const onPreviewReport = async (reportType: "morning" | "evening") => {
    setReportBusy(true);
    setReportLoading({ type: reportType, startedAt: Date.now() });
    setReportElapsed(0);
    try {
      const res = await runCrashDailyReport(reportType, { top_n: 5, dry_run: true });
      setReportModal({
        title: reportType === "morning" ? t("早报预览") : t("晚报预览"),
        preview: res.preview || "(empty)",
        reportType,
      });
    } catch (e: any) {
      setToast({ msg: e.message || "preview failed", type: "error" });
    } finally {
      setReportBusy(false);
      setReportLoading(null);
    }
  };

  const onSendReport = async () => {
    if (!reportModal) return;
    setReportBusy(true);
    try {
      const res = await runCrashDailyReport(reportModal.reportType, { top_n: 5, dry_run: false });
      if (res.sent) {
        setToast({ msg: t("已发送到飞书群"), type: "success" });
        setReportModal(null);
      } else {
        const reason = res.skipped_reason || "unknown";
        const hint =
          reason === "no_target_chat_id"
            ? t("请在 config.yaml 配置 feishu.target_chat_id")
            : reason === "feishu_disabled"
              ? t("飞书已禁用：config.yaml 设 feishu_enabled=true")
              : reason === "send_failed_or_no_chat"
                ? t("发送失败：检查 .env 的 FEISHU_APP_ID / FEISHU_APP_SECRET")
                : reason;
        setReportModal({ ...reportModal, error: hint });
        setToast({ msg: `${t("发送失败")}: ${hint}`, type: "error" });
      }
    } catch (e: any) {
      const msg = e.message || "send failed";
      setReportModal({ ...reportModal, error: msg });
      setToast({ msg, type: "error" });
    } finally {
      setReportBusy(false);
    }
  };

  // Batch top N（从 localStorage 读，设置页可改；跨页广播自动同步）
  const [batchTopN, setBatchTopNState] = useState<number>(20);
  useEffect(() => {
    setBatchTopNState(getBatchTopN());
    const onChange = (e: Event) => {
      const ce = e as CustomEvent<number>;
      if (typeof ce.detail === "number") setBatchTopNState(ce.detail);
    };
    window.addEventListener("crashguard:batch_top_n_changed", onChange as EventListener);
    return () =>
      window.removeEventListener("crashguard:batch_top_n_changed", onChange as EventListener);
  }, []);

  const onBatchAnalyze = async () => {
    const n = batchTopN;
    if (!confirm(t("批量启动 AI 分析（仅未分析过的 Top N）。继续？").replace("Top N", `Top ${n}`))) return;
    setBatching(true);
    try {
      const res = await batchAnalyzeCrash(n);
      setToast({
        msg: `${t("批量启动")}: ${t("已调度")}=${res.scheduled.length} | ${t("跳过")}=${res.skipped.length}`,
        type: "success",
      });
      await loadTop();
    } catch (e: any) {
      setToast({ msg: e.message || "batch failed", type: "error" });
    } finally {
      setBatching(false);
    }
  };

  // 列表筛选/分页/排序 任意变化 → 重新拉
  useEffect(() => {
    loadTop();
  }, [loadTop]);

  // 自动刷新：每次打开页面都先展示本地数据（loadTop 已完成），再异步拉服务端最新
  // 设计要点：
  //   - 本地优先：loadTop 同步先跑（上面 effect），用户立刻看到数据
  //   - 服务端兜底：触发 /warmup 拉 Datadog；3 秒返回，AI 分析后台跑
  //   - 冷却保护：localStorage 记录上次拉取时间，CRASHGUARD_REFRESH_COOLDOWN_MIN 内不重复
  //   - 空数据强刷：忽略冷却（首次/重置后必须拉）
  //   - bootstrapDone 单次保护：本次会话只触发一次，避免路由切换反复拉
  useEffect(() => {
    if (loading) return;
    if (bootstrapping || bootstrapDone) return;
    if (!datadogConfigured) return;

    const cooldownMin = 5;
    const lastKey = "crashguard_last_refresh";
    let last = 0;
    try {
      last = Number(localStorage.getItem(lastKey) || 0);
    } catch {}
    const ageMin = (Date.now() - last) / 60_000;
    const isEmpty = items.length === 0;
    if (!isEmpty && ageMin < cooldownMin) {
      setBootstrapDone(true);
      return;
    }

    setBootstrapping(true);
    setBootstrapElapsed(0);
    const startedAt = Date.now();
    const timer = setInterval(
      () => setBootstrapElapsed(Math.floor((Date.now() - startedAt) / 1000)),
      500,
    );
    triggerCrashWarmup()
      .then(async (r) => {
        try { localStorage.setItem(lastKey, String(Date.now())); } catch {}
        const bg = (r as any).ai_background;
        setToast({
          msg: isEmpty
            ? `已拉取 ${r.issues_processed} 条 issue${bg ? "，AI 分析后台运行中" : ""}`
            : `已自动同步最新数据 (${r.issues_processed} 条)`,
          type: "success",
        });
        await loadTop();
      })
      .catch((e) => {
        // bootstrap 是后台自动同步，非用户主动操作——abort/超时/网络抖动不该弹错误 toast
        // 命中即静默（数据没拉到也无所谓，下次进页面会重试；AI 分析仍在后台跑）
        // 各浏览器对 abort 的报错形态：
        //   Chrome: DOMException name=AbortError msg="signal is aborted without reason"
        //   Safari/Firefox: AbortError 但 msg 不同
        //   网络抖：TypeError msg="Failed to fetch"
        const name = e?.name || "";
        const msg = e?.message || String(e);
        if (name === "AbortError" || /abort/i.test(msg) || msg === "Failed to fetch") {
          return;
        }
        setToast({ msg: `自动拉取失败：${msg}`, type: "error" });
      })
      .finally(() => {
        clearInterval(timer);
        setBootstrapping(false);
        setBootstrapDone(true);
      });
  }, [loading, items.length, datadogConfigured, bootstrapping, bootstrapDone]);

  // 自动 PR 队列状态（每 30s 刷新一次，让用户实时看到进度）
  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const q = await fetchAutoPrQueue();
        if (!cancelled) setAutoPrQueue(q);
      } catch {
        // 静默——队列接口不可用不阻塞主流程
      }
    };
    tick();
    const id = setInterval(tick, 30000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  // 「线上最新版本」+ 版本分布饼图 + 机型分布——按平台从后端并发拉
  useEffect(() => {
    let cancelled = false;
    Promise.all([
      fetchCrashLatestRelease(),
      fetchCrashVersionDistribution(24),
      fetchCrashOsVersionDistribution(24),
      fetchCrashPlatformSummary(24),
    ])
      .then(([r, vd, od, ps]) => {
        if (cancelled) return;
        setLatestRelease(r.versions);
        setLatestReleaseSource(r.source as any);
        setTopUserVersion(r.top_user_versions ?? null);
        setTopUserVersionSource(r.top_user_versions_source ?? null);
        setVersionDistribution(vd.data ?? {});
        setOsVersionDistribution(od.data ?? {});
        setPlatformSummary(ps.data ?? {});
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [items.length]);

  useEffect(() => {
    if (!reportLoading) return;
    const id = setInterval(() => {
      setReportElapsed(Math.floor((Date.now() - reportLoading.startedAt) / 1000));
    }, 500);
    return () => clearInterval(id);
  }, [reportLoading]);

  // Deep link：URL ?issue=<id> 同步打开抽屉（含初次加载 + 浏览器返回前进）
  useEffect(() => {
    const urlIssue = searchParams?.get("issue") || null;
    if (urlIssue && urlIssue !== selectedId) {
      void loadDetail(urlIssue);
    } else if (!urlIssue && selectedId) {
      // URL 没了 → 关抽屉（如浏览器返回）
      setSelectedId(null);
      setDetail(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  return (
    <div className="flex h-full" style={{ background: D.bg, color: D.text1 }}>
      {/* Main pane */}
      <div className="flex-1 flex flex-col min-w-0 overflow-auto">
        {/* Top filter bar (Firebase style) — 三行合一：筛选 + 时间窗口 + 操作按钮 */}
        <div
          className="flex items-center justify-between gap-3 px-6 py-3 flex-wrap"
          style={{ background: D.bg, borderBottom: `1px solid ${D.border}` }}
        >
          {/* Left: 筛选 pills + 时间窗口 pills */}
          <div className="flex items-center gap-2 flex-wrap">
            <Pill icon="filter" label={t("筛选")} />
            <FilterPill
              label={t("平台")}
              value={platformFilter}
              onChange={setPlatformFilter}
              options={[{ v: "all", l: t("全部") }, ...platforms.map((p) => ({ v: p, l: platformLabel(p) }))]}
            />
            <FilterPill
              label={t("类型")}
              value={fatalityFilter}
              onChange={(v) => setFatalityFilter(v as any)}
              options={[
                { v: "all", l: t("全部") },
                { v: "fatal", l: t("🔴 严重崩溃") },
                { v: "non_fatal", l: t("⚠️ 业务失败") },
              ]}
            />
            <FilterPill
              label={t("等级")}
              value={tierFilter}
              onChange={(v) => setTierFilter(v as any)}
              options={[
                { v: "all", l: t("全部") },
                { v: "P0", l: "P0" },
                { v: "P1", l: "P1" },
              ]}
            />
            <FilterPill
              label={t("状态")}
              value={statusFilter}
              onChange={(v) => setStatusFilter(v as any)}
              options={[
                { v: "all", l: t("全部") },
                ...STATUS_OPTIONS.map((s) => ({ v: s.value, l: t(s.label) })),
              ]}
            />
            <FilterPill
              label={t("排序")}
              value={sortBy}
              onChange={(v) => setSortBy(v as any)}
              options={[
                { v: "impact", l: t("影响分") },
                { v: "events", l: t("事件数") },
                { v: "users", l: t("用户数") },
                { v: "new_first", l: t("新增/回归优先") },
              ]}
            />
            {/* 分隔 + 时间窗口 */}
            <span className="mx-1" style={{ color: D.text3 }}>|</span>
            <span className="text-xs" style={{ color: D.text3 }}>⏱</span>
            {([
              [24, t("Last 1d")],
              [168, t("Last 7d")],
              [336, t("Last 14d")],
              [720, t("Last 30d")],
            ] as [24 | 168 | 336 | 720, string][]).map(([w, label]) => (
              <button
                key={w}
                onClick={() => setWindowHours(w)}
                className="rounded px-2 py-0.5 text-xs"
                style={{
                  background: windowHours === w ? D.accent + "33" : "transparent",
                  border: `1px solid ${windowHours === w ? D.accent : D.border}`,
                  color: windowHours === w ? D.text1 : D.text2,
                }}
              >
                {label}
              </button>
            ))}
          </div>

          {/* Right: 操作按钮 (warning + 6 buttons) */}
          <div className="flex items-center gap-2 flex-wrap">
            {datadogConfigured === false && (
              <span
                className="text-xs px-2 py-1 rounded"
                style={{ color: D.danger, background: D.warnBg, border: `1px solid ${D.danger}55` }}
              >
                ⚠ {t("Datadog 未配置")}
              </span>
            )}
            <button
              onClick={onBatchAnalyze}
              disabled={batching}
              className="rounded px-2.5 py-1 text-xs font-medium inline-flex items-center gap-1"
              style={{
                background: "transparent",
                border: `1px solid ${D.borderStrong}`,
                color: D.text1,
                opacity: batching ? 0.5 : 1,
              }}
              title={t("对今日 Top N（未分析过的）批量启动 AI 分析")}
            >
              {batching ? t("批量分析中...") : `🤖 ${t("批量分析 Top")} ${batchTopN}`}
            </button>
            <button
              onClick={() => onPreviewReport("morning")}
              disabled={reportBusy}
              className="rounded px-2.5 py-1 text-xs font-medium inline-flex items-center gap-1"
              style={{
                background: "transparent",
                border: `1px solid ${D.borderStrong}`,
                color: D.text1,
                opacity: reportBusy ? 0.5 : 1,
              }}
              title={t("预览早报，确认无误后可一键推送到飞书群")}
            >
              🌅 {t("早报")}
            </button>
            <button
              onClick={() => onPreviewReport("evening")}
              disabled={reportBusy}
              className="rounded px-2.5 py-1 text-xs font-medium inline-flex items-center gap-1"
              style={{
                background: "transparent",
                border: `1px solid ${D.borderStrong}`,
                color: D.text1,
                opacity: reportBusy ? 0.5 : 1,
              }}
              title={t("预览晚报，确认无误后可一键推送到飞书群")}
            >
              🌇 {t("晚报")}
            </button>
            <a
              href="/crashguard/reports"
              className="rounded px-2.5 py-1 text-xs font-medium inline-flex items-center gap-1"
              style={{
                background: "transparent",
                border: `1px solid ${D.borderStrong}`,
                color: D.text1,
                textDecoration: "none",
              }}
              title={t("查看历史早晚报")}
            >
              📋 {t("历史报告")}
            </a>
            <a
              href="/crashguard/pull-requests"
              className="rounded px-2.5 py-1 text-xs font-medium inline-flex items-center gap-1"
              style={{
                background: "transparent",
                border: `1px solid ${D.borderStrong}`,
                color: D.text1,
                textDecoration: "none",
              }}
              title={
                autoPrQueue
                  ? `pending=${autoPrQueue.summary.pending} · running=${autoPrQueue.summary.running} · failed=${autoPrQueue.summary.recent_failures}`
                  : t("查看 AI 自动创建的 draft PR")
              }
            >
              🔧 {t("自动 PR")}
              {autoPrQueue && (autoPrQueue.summary.pending > 0 || autoPrQueue.summary.running > 0) && (
                <span
                  style={{
                    marginLeft: 4,
                    padding: "1px 6px",
                    borderRadius: 999,
                    fontSize: 10,
                    fontWeight: 700,
                    background: autoPrQueue.summary.running > 0 ? "#2563EB" : "#D97706",
                    color: "#FFFFFF",
                  }}
                >
                  {autoPrQueue.summary.running > 0
                    ? `${autoPrQueue.summary.running}⚙`
                    : `${autoPrQueue.summary.pending}⏳`}
                </span>
              )}
            </a>
            <a
              href="/crashguard/jobs"
              className="rounded px-2.5 py-1 text-xs font-medium inline-flex items-center gap-1"
              style={{
                background: "transparent",
                border: `1px solid ${D.borderStrong}`,
                color: D.text1,
                textDecoration: "none",
              }}
              title={t("查看 cron 任务进度、下次调度时间与失败历史")}
            >
              📊 {t("任务监控")}
            </a>
          </div>
        </div>

        {/* KPI 顶栏（方案 C）：4 列全局指标快览 — 始终展示全量，不受 fatality filter 影响 */}
        <div className="grid grid-cols-4 gap-3 px-6 mt-3">
          {(() => {
            // 全量数据源：fatalityFilter==="all" 时 globalAggregates===aggregates，否则是独立拉的全量
            const g = globalAggregates;
            const fatalEvents = (g as { fatal_events?: number } | null)?.fatal_events ?? 0;
            const nonFatalEvents = (g as { non_fatal_events?: number } | null)?.non_fatal_events ?? 0;
            const fatalCount = g?.fatal_count ?? 0;
            const nonFatalCount = g?.non_fatal_count ?? 0;
            // User 维度主指标（2026-05-21 切换），session 维度作 hint
            const gAny = g as {
              crash_free_users_pct?: number | null;
              crash_free_total_users?: number;
              crash_free_sessions_pct?: number | null;
              crash_free_total_sessions?: number;
            } | null;
            const cfUsersPct = gAny?.crash_free_users_pct ?? null;
            const cfTotalUsers = gAny?.crash_free_total_users ?? 0;
            const cfSessionsPct = gAny?.crash_free_sessions_pct ?? null;
            const cfTotalSess = gAny?.crash_free_total_sessions ?? 0;
            // 主指标优先 user，失败回退 session（首次或 Datadog 拉失败时）
            const primaryPct = cfUsersPct ?? cfSessionsPct;
            const primaryLabel = cfUsersPct != null ? t("Crash-free Users") : t("Crash-free Sessions");
            const primaryHint = cfUsersPct != null
              ? (
                  cfTotalUsers > 0
                    ? `${compactNumber(cfTotalUsers)} ${t("users (suffix)")}` + (
                        cfSessionsPct != null
                          ? ` · ${t("sessions (suffix)")} ${cfSessionsPct.toFixed(2)}%`
                          : ""
                      )
                    : t("窗口内无数据")
                )
              : (cfTotalSess > 0 ? `${compactNumber(cfTotalSess)} sessions` : t("窗口内无数据"));
            const totalIssueCount = (g?.fatal_count ?? 0) + (g?.non_fatal_count ?? 0);
            const p0 = g?.p0_count ?? 0;
            const surge = g?.surge_count ?? 0;
            return (
              <>
                <KpiStripCell
                  label={primaryLabel}
                  value={primaryPct != null ? `${primaryPct.toFixed(2)}%` : "—"}
                  hint={primaryHint}
                  accent={D.ok}
                />
                <KpiStripCell
                  label={t("严重崩溃")}
                  value={fatalEvents.toLocaleString()}
                  hint={`${fatalCount} issue · native + ANR + Hang`}
                  accent={D.danger}
                  active={fatalityFilter === "fatal"}
                  onClick={() => setFatalityFilter(fatalityFilter === "fatal" ? "all" : "fatal")}
                />
                <KpiStripCell
                  label={t("业务异常")}
                  value={nonFatalEvents.toLocaleString()}
                  hint={`${nonFatalCount} issue · addError / zone guard`}
                  accent={D.warn}
                  active={fatalityFilter === "non_fatal"}
                  onClick={() => setFatalityFilter(fatalityFilter === "non_fatal" ? "all" : "non_fatal")}
                />
                <KpiStripCell
                  label={t("Active Issues")}
                  value={totalIssueCount.toString()}
                  hint={`P0 ${p0} · ${t("飙升")} ${surge}`}
                  accent={D.accent}
                />
              </>
            );
          })()}
        </div>

        {/* 平台总览（方案 D）：iOS 在前 + Android */}
        <div className="grid grid-cols-2 gap-3 px-6 mt-3">
          <PlatformOverviewCard
            label="iOS"
            accent={D.p1}
            mainVersion={versionDistribution.ios?.[0]?.version}
            mainVersionPct={versionDistribution.ios?.[0]?.pct}
            versions={versionDistribution.ios ?? []}
            osVersions={osVersionDistribution.ios ?? []}
            summary={platformSummary.ios}
          />
          <PlatformOverviewCard
            label="Android"
            accent={D.ok}
            mainVersion={versionDistribution.android?.[0]?.version}
            mainVersionPct={versionDistribution.android?.[0]?.pct}
            versions={versionDistribution.android ?? []}
            osVersions={osVersionDistribution.android ?? []}
            summary={platformSummary.android}
          />
        </div>

        {/* Issues table */}
        <div className="px-6 mt-5 mb-4">
          <div className="flex items-center justify-between mb-2">
            <div className="text-sm font-semibold" style={{ color: D.text1 }}>
              {t("Issues")}
              <span className="ml-2 text-xs" style={{ color: D.text2 }}>
                · {filteredItems.length} {t("条")}
              </span>
            </div>
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder={t("搜索 issue 标题或 ID")}
              className="rounded px-3 py-1.5 text-xs w-72"
              style={{
                background: D.surface,
                border: `1px solid ${D.border}`,
                color: D.text1,
                outline: "none",
              }}
            />
          </div>

          <div
            className="rounded-lg overflow-hidden"
            style={{ background: D.surface, border: `1px solid ${D.border}` }}
          >
            <table className="w-full text-sm">
              <thead>
                <tr
                  style={{
                    color: D.text2,
                    borderBottom: `1px solid ${D.border}`,
                    fontSize: 11,
                    textTransform: "uppercase",
                    letterSpacing: "0.04em",
                  }}
                >
                  <th className="text-left px-4 py-2 font-medium">{t("Issue")}</th>
                  <th className="text-left px-3 py-2 font-medium" style={{ width: 220 }}>
                    {t("版本范围")}
                  </th>
                  <th className="text-left px-3 py-2 font-medium" style={{ width: 130 }}>
                    {t("状态")}
                  </th>
                  <th className="text-left px-3 py-2 font-medium" style={{ width: 110 }}>
                    {t("指派人")}
                  </th>
                  <th className="text-right px-3 py-2 font-medium" style={{ width: 80 }}>
                    {t("事件")}↓
                  </th>
                  <th className="text-right px-3 py-2 font-medium" style={{ width: 80 }}>
                    {t("会话")}
                  </th>
                  <th className="text-center px-3 py-2 font-medium" style={{ width: 90 }}>
                    PR
                  </th>
                  <th className="text-center px-3 py-2 font-medium" style={{ width: 50 }}>
                    DD
                  </th>
                </tr>
              </thead>
              <tbody>
                {filteredItems.length === 0 && !loading && (
                  <tr>
                    <td colSpan={8} className="px-6 py-12 text-center" style={{ color: D.text3 }}>
                      {bootstrapping
                        ? `⏳ ${t("正在从 Datadog 拉取最新崩溃数据并触发 AI 分析...")} ${bootstrapElapsed}s`
                        : (datadogConfigured
                            ? t("暂无数据。系统会自动拉取——若长时间未刷新，可点【刷新】重试")
                            : t("Datadog 未配置，无法自动拉取数据"))}
                    </td>
                  </tr>
                )}
                {filteredItems.map((it, idx) => {
                  const active = selectedId === it.datadog_issue_id;
                  const autoPrThreshold = autoPrQueue?.threshold ?? 0.7;
                  const feasibility = typeof it.analysis_feasibility_score === "number"
                    ? it.analysis_feasibility_score
                    : null;
                  const blocksAutoPr = !it.has_pr && feasibility !== null && feasibility < autoPrThreshold;
                  return (
                    <tr
                      key={it.datadog_issue_id}
                      onClick={() => loadDetail(it.datadog_issue_id)}
                      className="cursor-pointer transition-colors"
                      style={{
                        background: active ? D.accentBg : idx % 2 === 0 ? D.surface : D.surfaceAlt,
                        borderBottom: `1px solid ${D.border}`,
                      }}
                      onMouseEnter={(e) => {
                        if (!active) (e.currentTarget as HTMLElement).style.background = D.hover;
                      }}
                      onMouseLeave={(e) => {
                        if (!active)
                          (e.currentTarget as HTMLElement).style.background = idx % 2 === 0 ? D.surface : D.surfaceAlt;
                      }}
                    >
                      <td className="px-4 py-2.5">
                        <div className="flex items-start gap-2.5">
                          {/* crash dot + tier */}
                          <div className="flex flex-col items-center pt-0.5" style={{ width: 18 }}>
                            <span
                              className="inline-flex items-center justify-center h-4 w-4 rounded-full text-[9px] font-bold"
                              style={{ background: D.dangerBg, color: D.danger }}
                              title="Crash"
                            >
                              ✕
                            </span>
                            <span
                              className="text-[9px] font-bold mt-0.5"
                              style={{ color: tierColor(it.tier) }}
                            >
                              {it.tier}
                            </span>
                          </div>
                          <div className="min-w-0 flex-1">
                            <div className="flex items-center gap-2 flex-wrap">
                              <span className="text-xs font-mono" style={{ color: D.text2 }}>
                                {it.service || platformLabel(it.platform)}
                              </span>
                              <span style={{ color: D.text3 }}>·</span>
                              <span className="text-xs" style={{ color: D.text3 }}>
                                {platformLabel(it.platform)}
                              </span>
                              {/* C 路线：fatality tag */}
                              {it.fatality === "non_fatal" ? (
                                <Badge fg={D.warn} bg={D.warnBg}>
                                  ⚠️ {t("业务失败")}
                                </Badge>
                              ) : (
                                <Badge fg={D.danger} bg={D.dangerBg}>
                                  🔴 {t("崩溃")}
                                </Badge>
                              )}
                              {it.is_regression && (
                                <Badge fg={D.warn} bg={D.warnBg}>
                                  ↩ {t("回归")}
                                </Badge>
                              )}
                              {it.is_surge && (
                                <Badge fg={D.danger} bg={D.dangerBg}>
                                  ↗ {t("飙升")}
                                </Badge>
                              )}
                              {it.is_new_in_version && (
                                <Badge fg={D.accent} bg={D.accentBg}>
                                  ✨ {t("新增")}
                                </Badge>
                              )}
                              {it.first_analyzed_at && (
                                <Badge fg="#7C3AED" bg="rgba(167,139,250,0.12)">
                                  🤖 {t("已分析")}
                                </Badge>
                              )}
                              {blocksAutoPr && (
                                <span title={`${t("可行度低于自动 PR 阈值")}: ${(feasibility * 100).toFixed(0)}% < ${(autoPrThreshold * 100).toFixed(0)}%`}>
                                  <Badge fg={D.warn} bg={D.warnBg}>
                                    {t("PR 不自动生成")}
                                  </Badge>
                                </span>
                              )}
                            </div>
                            <div
                              className="text-sm mt-0.5 truncate"
                              style={{ color: D.text1, maxWidth: 520 }}
                            >
                              {it.title || "—"}
                            </div>
                          </div>
                        </div>
                      </td>
                      <td className="px-3 py-2.5">
                        <span
                          className="inline-block rounded px-2 py-0.5 text-[11px] font-mono"
                          style={{
                            color: D.text2,
                            background: D.surfaceAlt,
                            border: `1px solid ${D.border}`,
                          }}
                          title={`${it.first_seen_version || "—"} → ${it.last_seen_version || "—"}`}
                        >
                          {versionRange(it.first_seen_version, it.last_seen_version)}
                        </span>
                      </td>
                      <td className="px-3 py-2.5" onClick={(e) => e.stopPropagation()}>
                        <StatusSelect
                          value={it.status}
                          disabled={savingPatch}
                          onChange={(v) => onPatch(it.datadog_issue_id, { status: v })}
                          t={t}
                        />
                      </td>
                      <td className="px-3 py-2.5" onClick={(e) => e.stopPropagation()}>
                        <AssigneeInput
                          value={it.assignee}
                          disabled={savingPatch}
                          onSave={(v) => onPatch(it.datadog_issue_id, { assignee: v })}
                        />
                      </td>
                      <td className="px-3 py-2.5 text-right tabular-nums" style={{ color: D.text1 }}>
                        {compactNumber(it.events_count)}
                      </td>
                      <td className="px-3 py-2.5 text-right tabular-nums" style={{ color: D.text1 }}>
                        {compactNumber(it.sessions_affected)}
                      </td>
                      <td className="px-3 py-2.5 text-center" onClick={(e) => e.stopPropagation()}>
                        {it.has_pr && it.pr_url ? (
                          (() => {
                            const sc = (() => {
                              switch (it.pr_status) {
                                case "merged": return { fg: "#16A34A", bg: "rgba(22,163,74,0.10)", border: "#16A34A" };
                                case "open":   return { fg: "#2563EB", bg: "rgba(37,99,235,0.10)", border: "#2563EB" };
                                case "closed": return { fg: "#9CA3AF", bg: "rgba(0,0,0,0.05)",     border: "#9CA3AF" };
                                case "draft":
                                default:       return { fg: "#D97706", bg: "rgba(217,119,6,0.10)", border: "#D97706" };
                              }
                            })();
                            return (
                              <a
                                href={it.pr_url}
                                target="_blank"
                                rel="noopener noreferrer"
                                title={`${it.pr_repo || ""} #${it.pr_number ?? ""} · ${it.pr_status}`}
                                className="inline-flex items-center justify-center rounded px-1.5 py-0.5 text-[10px] font-semibold"
                                style={{
                                  background: sc.bg,
                                  color: sc.fg,
                                  border: `1px solid ${sc.border}`,
                                  textDecoration: "none",
                                  textTransform: "uppercase",
                                  minWidth: 60,
                                }}
                              >
                                {it.pr_status || "draft"}
                              </a>
                            );
                          })()
                        ) : (
                          (() => {
                            // 无 PR 时显示 blocker reason 小徽章，让 owner 一眼知道为什么没 PR
                            const blocker = (it as any).pr_blocker as
                              | { reason: string; label: string; hint: string }
                              | undefined;
                            if (!blocker) {
                              return <span style={{ color: D.text3, fontSize: 11 }}>—</span>;
                            }
                            const colorMap: Record<string, { fg: string; bg: string; border: string }> = {
                              no_analysis:       { fg: "#5B6470", bg: "rgba(0,0,0,0.05)",     border: "#9CA3AF" },
                              low_feasibility:   { fg: "#D97706", bg: "rgba(217,119,6,0.10)", border: "#D97706" },
                              low_confidence:    { fg: "#D97706", bg: "rgba(217,119,6,0.10)", border: "#D97706" },
                              gate_check_failed: { fg: "#DC2626", bg: "rgba(220,38,38,0.10)", border: "#DC2626" },
                              has_closed_pr:     { fg: "#5B6470", bg: "rgba(0,0,0,0.05)",     border: "#9CA3AF" },
                            };
                            const c = colorMap[blocker.reason] || { fg: D.text3, bg: "transparent", border: D.border };
                            return (
                              <span
                                title={blocker.hint || blocker.reason}
                                className="inline-flex items-center justify-center rounded px-1.5 py-0.5 text-[10px] font-medium"
                                style={{
                                  background: c.bg,
                                  color: c.fg,
                                  border: `1px solid ${c.border}`,
                                  minWidth: 60,
                                  whiteSpace: "nowrap",
                                }}
                              >
                                {blocker.label}
                              </span>
                            );
                          })()
                        )}
                      </td>
                      <td className="px-3 py-2.5 text-center" onClick={(e) => e.stopPropagation()}>
                        <a
                          href={it.datadog_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          title={t("在 Datadog 中打开")}
                          className="inline-flex items-center justify-center rounded h-6 w-6 text-xs"
                          style={{
                            background: D.accentBg,
                            color: D.accent,
                            textDecoration: "none",
                          }}
                        >
                          ↗
                        </a>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>

            {/* Pagination footer */}
            {totalPages > 1 && (
              <div
                className="flex items-center justify-center gap-3 py-4 border-t"
                style={{ borderColor: D.border }}
              >
                <button
                  onClick={() => setPage(Math.max(1, page - 1))}
                  disabled={page <= 1 || loading}
                  className="rounded px-3 py-1.5 text-xs"
                  style={{
                    border: `1px solid ${D.border}`,
                    background: D.surface,
                    color: page <= 1 ? D.text3 : D.text1,
                    cursor: page <= 1 ? "not-allowed" : "pointer",
                    opacity: page <= 1 ? 0.5 : 1,
                  }}
                >
                  ← {t("上一页")}
                </button>
                <span className="text-xs" style={{ color: D.text2 }}>
                  {page} / {totalPages}  ·  {totalCount} {t("条")}
                </span>
                <button
                  onClick={() => setPage(Math.min(totalPages, page + 1))}
                  disabled={page >= totalPages || loading}
                  className="rounded px-3 py-1.5 text-xs"
                  style={{
                    border: `1px solid ${D.border}`,
                    background: D.surface,
                    color: page >= totalPages ? D.text3 : D.text1,
                    cursor: page >= totalPages ? "not-allowed" : "pointer",
                    opacity: page >= totalPages ? 0.5 : 1,
                  }}
                >
                  {t("下一页")} →
                </button>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Detail drawer */}
      {selectedId && (
        <DetailDrawer
          loading={detailLoading}
          detail={detail}
          analyses={analyses}
          followupText={followupText}
          followupSubmitting={followupSubmitting}
          onFollowupChange={setFollowupText}
          onFollowupSubmit={() => startFollowup(selectedId!, followupText)}
          savingPatch={savingPatch}
          analyzing={analyzing}
          onAnalyze={() => {
            setReanalyzePrompt("");
            setReanalyzeModal({ issueId: selectedId! });
          }}
          onPatch={(patch) => onPatch(selectedId!, patch)}
          onClose={() => {
            setSelectedId(null);
            setDetail(null);
            syncSelectedToUrl(null);
          }}
          creatingPr={creatingPr}
          onCreatePr={onCreatePr}
          autoPrThreshold={autoPrQueue?.threshold ?? 0.7}
          diagStatus={diagStatus}
          diagLoading={diagLoading}
          diagConfirming={diagConfirming}
          onStartDeepAnalysis={onStartDeepAnalysis}
          onConfirmHypothesis={onConfirmHypothesis}
          t={t}
        />
      )}

      {toast && <Toast msg={toast.msg} type={toast.type} onClose={() => setToast(null)} />}

      {reportLoading && !reportModal && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center"
          style={{ background: "rgba(0,0,0,0.45)" }}
        >
          <div
            className="rounded-lg shadow-xl px-6 py-5"
            style={{
              background: D.surface,
              width: 420,
              border: `1px solid ${D.border}`,
            }}
          >
            <div className="flex items-center gap-2 mb-3">
              <span className="text-base">
                {reportLoading.type === "morning" ? "🌅" : "🌇"}
              </span>
              <div className="text-sm font-semibold" style={{ color: D.text1 }}>
                {reportLoading.type === "morning" ? t("生成早报中...") : t("生成晚报中...")}
              </div>
              <span className="ml-auto text-xs tabular-nums" style={{ color: D.text2 }}>
                {reportElapsed}s
              </span>
            </div>
            <div
              className="relative h-1.5 rounded-full overflow-hidden mb-3"
              style={{ background: "rgba(0,0,0,0.06)" }}
            >
              <div
                className="absolute inset-y-0 rounded-full"
                style={{
                  width: "40%",
                  background: `linear-gradient(90deg, transparent, ${D.accent}, transparent)`,
                  animation: "crashguard-indeterminate 1.4s linear infinite",
                }}
              />
            </div>
            <div className="text-xs leading-relaxed" style={{ color: D.text2 }}>
              {reportElapsed < 5
                ? t("正在拉取 Datadog 崩溃数据…")
                : reportElapsed < 15
                ? t("正在排序 Top N 并匹配历史…")
                : reportElapsed < 30
                ? t("正在生成 Markdown 报告…")
                : t("接近完成，请稍候（首次拉取耗时较长）…")}
            </div>
          </div>
          <style jsx global>{`
            @keyframes crashguard-indeterminate {
              0% { left: -40%; }
              100% { left: 100%; }
            }
          `}</style>
        </div>
      )}

      {/* 重新分析 modal：让用户输入 prompt 引导 AI 分析方向 */}
      {reanalyzeModal && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center"
          style={{ background: "rgba(0,0,0,0.45)" }}
          onClick={() => setReanalyzeModal(null)}
        >
          <div
            className="rounded-lg shadow-xl flex flex-col"
            style={{
              background: D.surface,
              width: 640,
              maxHeight: "85vh",
              border: `1px solid ${D.border}`,
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <div
              className="flex items-center justify-between px-5 py-3"
              style={{
                borderBottom: `1px solid ${D.border}`,
                background: "linear-gradient(135deg, #F5F3FF 0%, #FFFFFF 100%)",
              }}
            >
              <div className="flex items-center gap-2">
                <span className="text-base">🤖</span>
                <div className="text-sm font-semibold" style={{ color: D.text1 }}>
                  {t("重新分析")} · {t("引导 AI")}
                </div>
              </div>
              <button
                onClick={() => setReanalyzeModal(null)}
                className="rounded px-2 py-1 text-sm"
                style={{ color: D.text2 }}
              >
                ✕
              </button>
            </div>
            <div className="flex-1 overflow-auto px-5 py-4">
              <div className="text-xs mb-2" style={{ color: D.text2 }}>
                {t("可选——告诉 AI 你想让它重点关注的方向；留空则跑默认分析。")}
              </div>
              <textarea
                value={reanalyzePrompt}
                onChange={(e) => setReanalyzePrompt(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                    e.preventDefault();
                    submitReanalyze();
                  }
                }}
                placeholder={t("例：重点排查空指针 / 优先看 OOM 路径 / 关注最近版本回归 ...")}
                rows={6}
                className="w-full rounded px-3 py-2 text-[13px] leading-relaxed"
                style={{
                  border: `1px solid ${D.borderStrong}`,
                  background: D.surface,
                  color: D.text1,
                  outline: "none",
                  resize: "vertical",
                  fontFamily: "inherit",
                }}
                autoFocus
              />
              <div className="text-[11px] mt-2" style={{ color: D.text3 }}>
                ⌘/Ctrl + Enter {t("快速提交")}
              </div>
            </div>
            <div
              className="flex items-center justify-end gap-2 px-5 py-3"
              style={{ borderTop: `1px solid ${D.border}` }}
            >
              <button
                onClick={() => setReanalyzeModal(null)}
                className="rounded px-3 py-1.5 text-xs font-medium"
                style={{
                  background: "transparent",
                  border: `1px solid ${D.borderStrong}`,
                  color: D.text1,
                }}
              >
                {t("取消")}
              </button>
              <button
                onClick={submitReanalyze}
                className="rounded px-3 py-1.5 text-xs font-semibold"
                style={{
                  background: "#7C3AED",
                  color: "#FFFFFF",
                  border: "1px solid #7C3AED",
                }}
              >
                🚀 {reanalyzePrompt.trim() ? t("按引导分析") : t("默认分析")}
              </button>
            </div>
          </div>
        </div>
      )}

      {reportModal && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center"
          style={{ background: "rgba(0,0,0,0.45)" }}
          onClick={() => !reportBusy && setReportModal(null)}
        >
          <div
            className="rounded-lg shadow-xl flex flex-col"
            style={{
              background: D.surface,
              width: 880,
              maxHeight: "85vh",
              border: `1px solid ${D.border}`,
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <div
              className="flex items-center justify-between px-5 py-3"
              style={{
                borderBottom: `1px solid ${D.border}`,
                background: "linear-gradient(135deg, #FFFAF0 0%, #FFFFFF 100%)",
              }}
            >
              <div className="flex items-center gap-2">
                <span className="text-base">
                  {reportModal.reportType === "morning" ? "🌅" : "🌇"}
                </span>
                <div className="text-sm font-semibold" style={{ color: D.text1 }}>
                  {reportModal.title}
                </div>
                <span
                  className="rounded-full px-2 py-0.5 text-[10px]"
                  style={{ background: "#FEF3C7", color: "#92400E" }}
                >
                  {t("预览")}
                </span>
              </div>
              <button
                onClick={() => setReportModal(null)}
                className="rounded px-2 py-1 text-sm"
                style={{ color: D.text2 }}
              >
                ✕
              </button>
            </div>
            <div
              className="flex-1 overflow-auto px-6 py-5 text-[13px] leading-relaxed crashguard-md"
              style={{ color: D.text1, background: D.surface }}
            >
              <MarkdownText>{reportModal.preview}</MarkdownText>
            </div>
            {reportModal.error && (
              <div
                className="px-5 py-3 text-xs"
                style={{
                  borderTop: `1px solid ${D.border}`,
                  background: "rgba(239,68,68,0.08)",
                  color: "#B91C1C",
                }}
              >
                ⚠️ {reportModal.error}
              </div>
            )}
            <div
              className="flex items-center justify-end gap-2 px-5 py-3"
              style={{ borderTop: `1px solid ${D.border}` }}
            >
              <button
                onClick={() => setReportModal(null)}
                disabled={reportBusy}
                className="rounded px-3 py-1.5 text-xs font-medium"
                style={{
                  background: "transparent",
                  border: `1px solid ${D.borderStrong}`,
                  color: D.text1,
                }}
              >
                {t("取消")}
              </button>
              <button
                onClick={onSendReport}
                disabled={reportBusy}
                className="rounded px-3 py-1.5 text-xs font-medium"
                style={{
                  background: D.accent,
                  color: "#FFFFFF",
                  opacity: reportBusy ? 0.5 : 1,
                }}
              >
                {reportBusy ? t("发送中...") : `📨 ${t("推送到飞书群")}`}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function FilterPill({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: { v: string; l: string }[];
}) {
  const active = value !== "all";
  return (
    <label
      className="inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs cursor-pointer"
      style={{
        background: active ? D.accentBg : "transparent",
        border: `1px solid ${active ? D.accent : D.borderStrong}`,
        color: active ? D.accent : D.text2,
      }}
    >
      <span>{label}</span>
      <span style={{ opacity: 0.5 }}>=</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="bg-transparent outline-none cursor-pointer"
        style={{ color: "inherit" }}
      >
        {options.map((o) => (
          <option key={o.v} value={o.v} style={{ background: D.surface, color: D.text1 }}>
            {o.l}
          </option>
        ))}
      </select>
    </label>
  );
}

function Pill({ label }: { icon?: string; label: string }) {
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-xs font-medium"
      style={{ background: "transparent", border: `1px solid ${D.borderStrong}`, color: D.text2 }}
    >
      ⏷ {label}
    </span>
  );
}

function StatCardLarge({
  title,
  primary,
  secondary,
  hint,
  muted,
}: {
  title: string;
  primary: string;
  secondary: string;
  hint: string;
  muted?: boolean;
}) {
  return (
    <div
      className="rounded-lg px-4 py-3"
      style={{
        background: D.surface,
        border: `1px solid ${D.border}`,
        opacity: muted ? 0.55 : 1,
      }}
    >
      <div className="text-xs" style={{ color: D.text2 }}>
        {title}
      </div>
      <div className="text-2xl font-bold mt-1 tabular-nums" style={{ color: D.text1 }}>
        {primary}
      </div>
      <div className="text-xs mt-0.5" style={{ color: D.text2 }}>
        {secondary}
      </div>
      {hint && (
        <div className="text-[10px] mt-1" style={{ color: D.text3 }}>
          {hint}
        </div>
      )}
    </div>
  );
}

function TrendCard({
  title,
  value,
  hint,
  accent,
}: {
  title: string;
  value: string;
  hint: string;
  accent?: string;
}) {
  return (
    <div
      className="rounded-lg px-4 py-3"
      style={{ background: D.surface, border: `1px solid ${D.border}` }}
    >
      <div className="text-[11px]" style={{ color: D.text2 }}>
        {title}
      </div>
      <div className="text-xl font-bold mt-1 tabular-nums" style={{ color: accent || D.text1 }}>
        {value}
      </div>
      <div className="text-[10px] mt-0.5" style={{ color: D.text3 }}>
        {hint}
      </div>
    </div>
  );
}

function Badge({
  children,
  fg,
  bg,
}: {
  children: React.ReactNode;
  fg: string;
  bg: string;
}) {
  return (
    <span
      className="inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold"
      style={{ color: fg, background: bg }}
    >
      {children}
    </span>
  );
}

function StatusSelect({
  value,
  disabled,
  onChange,
  t,
}: {
  value: CrashStatus;
  disabled: boolean;
  onChange: (v: CrashStatus) => void;
  t: (k: string) => string;
}) {
  const c = STATUS_COLORS[value] || STATUS_COLORS.open;
  return (
    <select
      value={value}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value as CrashStatus)}
      className="rounded px-2 py-0.5 text-[11px] font-semibold cursor-pointer"
      style={{ background: c.bg, color: c.fg, border: "none", outline: "none" }}
    >
      {STATUS_OPTIONS.map((s) => (
        <option key={s.value} value={s.value} style={{ background: D.surface, color: D.text1 }}>
          {t(s.label)}
        </option>
      ))}
    </select>
  );
}

function AssigneeInput({
  value,
  disabled,
  onSave,
}: {
  value: string;
  disabled: boolean;
  onSave: (v: string) => void;
}) {
  const [v, setV] = useState(value);
  useEffect(() => {
    setV(value);
  }, [value]);
  const dirty = v !== value;
  const commit = () => {
    if (dirty) onSave(v.trim());
  };
  return (
    <input
      type="text"
      value={v}
      placeholder="—"
      disabled={disabled}
      onChange={(e) => setV(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") (e.currentTarget as HTMLInputElement).blur();
        if (e.key === "Escape") setV(value);
      }}
      className="rounded px-2 py-0.5 text-xs w-24"
      style={{
        background: dirty ? D.accentBg : "transparent",
        border: `1px solid ${dirty ? D.accent : D.border}`,
        color: D.text1,
        outline: "none",
      }}
    />
  );
}

function DetailDrawer({
  loading,
  detail,
  analyses,
  followupText,
  followupSubmitting,
  onFollowupChange,
  onFollowupSubmit,
  savingPatch,
  analyzing,
  onAnalyze,
  onPatch,
  onClose,
  creatingPr,
  onCreatePr,
  autoPrThreshold,
  diagStatus,
  diagLoading,
  diagConfirming,
  onStartDeepAnalysis,
  onConfirmHypothesis,
  t,
}: {
  loading: boolean;
  detail: CrashIssueDetail | null;
  analyses: CrashAnalysisRecord[];
  followupText: string;
  followupSubmitting: boolean;
  onFollowupChange: (v: string) => void;
  onFollowupSubmit: () => void;
  savingPatch: boolean;
  analyzing: boolean;
  onAnalyze: () => void;
  onPatch: (patch: { status?: CrashStatus; assignee?: string }) => void;
  onClose: () => void;
  creatingPr: number | null;
  onCreatePr: (analysisId: number, issueId: string) => void;
  autoPrThreshold: number;
  diagStatus: DiagnosisStatus | null;
  diagLoading: boolean;
  diagConfirming: string | null;
  onStartDeepAnalysis: (issueId: string) => void;
  onConfirmHypothesis: (runId: string, hypothesisId: string, issueId: string) => void;
  t: (k: string) => string;
}) {
  const [stackExpanded, setStackExpanded] = useState(false);
  const [stackVariantIdx, setStackVariantIdx] = useState(0);
  // Reset expansion when switching to a different issue
  useEffect(() => { setStackExpanded(false); setStackVariantIdx(0); }, [detail?.datadog_issue_id]);

  return (
    <div
      className="flex flex-col flex-shrink-0"
      style={{
        width: "40vw",
        minWidth: 560,
        maxWidth: 1200,
        background: D.surface,
        borderLeft: `1px solid ${D.border}`,
        color: D.text1,
      }}
    >
      <div
        className="flex items-center justify-between px-5 py-3"
        style={{ borderBottom: `1px solid ${D.border}` }}
      >
        <div className="text-sm font-semibold">{t("Issue 详情")}</div>
        <button
          onClick={onClose}
          className="rounded px-2 py-1 text-sm"
          style={{ color: D.text2 }}
        >
          ✕
        </button>
      </div>
      <div className="flex-1 overflow-auto px-5 py-4">
        {loading && <div style={{ color: D.text2 }}>{t("加载中...")}</div>}
        {!loading && detail && (
          <div className="space-y-5">
            <div>
              <div className="text-base font-semibold leading-snug" style={{ color: D.text1 }}>
                {detail.title || "—"}
              </div>
              <div className="text-xs mt-1 font-mono" style={{ color: D.text3 }}>
                {detail.datadog_issue_id}
              </div>
            </div>

            <div className="flex items-center gap-2 flex-wrap">
              <StatusSelect
                value={detail.status}
                disabled={savingPatch}
                onChange={(v) => onPatch({ status: v })}
                t={t}
              />
              <AssigneeInput
                value={detail.assignee}
                disabled={savingPatch}
                onSave={(v) => onPatch({ assignee: v })}
              />
              <a
                href={detail.datadog_url}
                target="_blank"
                rel="noopener noreferrer"
                className="rounded px-2.5 py-1 text-xs font-medium inline-flex items-center gap-1"
                style={{
                  background: D.accentBg,
                  color: D.accent,
                  border: `1px solid ${D.accent}`,
                  textDecoration: "none",
                }}
              >
                {t("在 Datadog 中打开")} ↗
              </a>
              {/* 自动化修复 PR — 与 Datadog 链接相邻，方便查找 */}
              {(() => {
                const ana = detail.analysis as any;
                const anaId = ana?.id as number | undefined;
                const feasibility = typeof ana?.feasibility_score === "number" ? ana.feasibility_score : null;
                const prs = detail.pull_requests || [];
                const openablePr = prs.find((p) => p.pr_url);
                const isLoading = creatingPr === anaId;
                if (openablePr) {
                  return null; // 下方 PR 标签链已显示，不重复渲染
                }
                if (!anaId) return null;
                if (feasibility !== null && feasibility < autoPrThreshold) {
                  return (
                    <span
                      className="rounded px-2.5 py-1 text-xs font-medium inline-flex items-center gap-1"
                      style={{
                        background: D.warnBg,
                        color: D.warn,
                        border: `1px solid ${D.warn}`,
                      }}
                      title={`${t("可行度低于自动 PR 阈值")}: ${(feasibility * 100).toFixed(0)}% < ${(autoPrThreshold * 100).toFixed(0)}%`}
                    >
                      ⚠ {t("可信度较低，无法自动生成 PR")}
                    </span>
                  );
                }
                return (
                  <button
                    onClick={() => onCreatePr(anaId, detail.datadog_issue_id)}
                    disabled={isLoading || creatingPr !== null}
                    className="rounded px-2.5 py-1 text-xs font-medium inline-flex items-center gap-1"
                    style={{
                      background: isLoading ? "#9CA3AF" : "#1F2937",
                      color: "#FFFFFF",
                      cursor: isLoading ? "wait" : "pointer",
                      opacity: creatingPr !== null && !isLoading ? 0.5 : 1,
                    }}
                    title={t("基于修复方案，自动生成 GitHub draft PR")}
                  >
                    {isLoading ? `⏳ ${t("生成中...")}` : `🔧 ${t("自动化修复 PR")}`}
                  </button>
                );
              })()}
              {(detail.pull_requests || []).slice(0, 3).map((pr) => {
                const sc = (() => {
                  switch (pr.pr_status) {
                    case "merged": return { fg: "#16A34A", bg: "rgba(22,163,74,0.10)", border: "#16A34A" };
                    case "open":   return { fg: "#2563EB", bg: "rgba(37,99,235,0.10)", border: "#2563EB" };
                    case "closed": return { fg: "#9CA3AF", bg: "rgba(0,0,0,0.05)",     border: "#9CA3AF" };
                    case "draft":
                    default:       return { fg: "#D97706", bg: "rgba(217,119,6,0.10)", border: "#D97706" };
                  }
                })();
                const label = pr.pr_number ? `#${pr.pr_number}` : "PR";
                return (
                  <a
                    key={pr.id}
                    href={pr.pr_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    title={`${pr.repo} · ${pr.pr_status}${pr.branch_name ? " · " + pr.branch_name : ""}`}
                    className="rounded px-2.5 py-1 text-xs font-medium inline-flex items-center gap-1"
                    style={{
                      background: sc.bg,
                      color: sc.fg,
                      border: `1px solid ${sc.border}`,
                      textDecoration: "none",
                    }}
                  >
                    {label} · {pr.pr_status} ↗
                  </a>
                );
              })}
              <button
                onClick={onAnalyze}
                disabled={analyzing}
                className="rounded px-2.5 py-1 text-xs font-semibold inline-flex items-center gap-1 ml-auto"
                style={{
                  background: analyzing ? "transparent" : "#7C3AED",
                  color: analyzing ? D.text2 : "#FFFFFF",
                  border: `1px solid ${analyzing ? D.borderStrong : "#7C3AED"}`,
                  opacity: analyzing ? 0.6 : 1,
                }}
                title={t("重新让 AI 分析这个 issue（30-90 秒）")}
              >
                {analyzing ? `⏳ ${t("分析中...")}` : `🤖 ${t("重新分析")}`}
              </button>
            </div>

            <Section title={t("基础信息")}>
              <div className="grid grid-cols-2 gap-x-6 gap-y-1.5">
                <KV k={t("平台")} v={platformLabel(detail.platform)} />
                <KV k={t("服务")} v={detail.service || "—"} />
                <KV k={t("版本范围")} v={versionRange(detail.first_seen_version, detail.last_seen_version)} />
                <KV k={t("总事件数")} v={detail.total_events.toLocaleString()} />
                <KV k={t("首次出现")} v={`${detail.first_seen_at?.replace("T", " ").slice(0, 16) || "—"}`} />
                <KV k={t("最近出现")} v={`${detail.last_seen_at?.replace("T", " ").slice(0, 16) || "—"}`} />
              </div>
              {/* 机型分布（保留文本一行，不上饼图） */}
              {detail.top_device && (
                <div className="mt-2 pt-2" style={{ borderTop: `1px dashed ${D.border}` }}>
                  <KV k={`📲 ${t("机型分布")}`} v={detail.top_device} multiline />
                </div>
              )}
            </Section>

            {/* OS 分布 + App 版本分布 → 双饼图并列 */}
            {(detail.top_os || detail.top_app_version) && (
              <Section title={t("分布")}>
                <div className="grid grid-cols-2 gap-3">
                  {detail.top_os && (
                    <PieChart
                      title={`📱 ${t("OS 分布")}`}
                      slices={parseDistribution(detail.top_os)}
                    />
                  )}
                  {detail.top_app_version && (
                    <PieChart
                      title={`🏷️ ${t("App 版本分布")}`}
                      slices={parseDistribution(detail.top_app_version)}
                    />
                  )}
                </div>
              </Section>
            )}

            <Section title={t("代表性堆栈")}>
              {(() => {
                const variants = (detail.stack_variants || []).filter((v) => v && v.representative_stack);
                const hasVariants = variants.length > 1;
                const activeIdx = hasVariants ? Math.min(stackVariantIdx, variants.length - 1) : 0;
                const active = hasVariants ? variants[activeIdx] : null;
                const raw = active ? active.representative_stack : (detail.representative_stack || "");
                const lines = raw.split("\n");
                const PREVIEW = 20;
                const hasMore = lines.length > PREVIEW;
                const visible = stackExpanded ? lines : lines.slice(0, PREVIEW);
                return (
                  <div>
                    {hasVariants && (
                      <>
                        <div
                          className="mb-2 text-[11px]"
                          style={{ color: D.text2 }}
                        >
                          {t("Datadog 此 issue 内混入")} <strong style={{ color: D.text1 }}>{variants.length}</strong> {t("种不同堆栈，按事件占比排序：")}
                        </div>
                        <div className="mb-2 flex flex-wrap gap-1.5">
                          {variants.map((v, i) => {
                            const isActive = i === activeIdx;
                            return (
                              <button
                                key={`${v.top_frame}-${i}`}
                                onClick={() => { setStackVariantIdx(i); setStackExpanded(false); }}
                                className="rounded px-2 py-1 text-[11px] font-mono"
                                title={v.top_frame}
                                style={{
                                  background: isActive ? D.accent : D.surfaceAlt,
                                  color: isActive ? "#fff" : D.text1,
                                  border: `1px solid ${isActive ? D.accent : D.border}`,
                                  cursor: "pointer",
                                  maxWidth: 360,
                                  overflow: "hidden",
                                  textOverflow: "ellipsis",
                                  whiteSpace: "nowrap",
                                }}
                              >
                                <span style={{ opacity: 0.75, marginRight: 4 }}>{v.pct}%</span>
                                <span>{v.top_frame}</span>
                                {v.is_main && <span style={{ marginLeft: 4, opacity: 0.7 }}>★</span>}
                              </button>
                            );
                          })}
                        </div>
                        {active && (
                          <div className="mb-2 text-[11px]" style={{ color: D.text2 }}>
                            <span>{t("事件数")}: <strong style={{ color: D.text1 }}>{active.count}</strong></span>
                            {active.sample_app_version && <span style={{ marginLeft: 12 }}>{t("版本")}: {active.sample_app_version}</span>}
                            {active.sample_view && <span style={{ marginLeft: 12 }}>{t("页面")}: {active.sample_view}</span>}
                          </div>
                        )}
                      </>
                    )}
                    <pre
                      className="rounded p-3 text-xs font-mono overflow-auto whitespace-pre"
                      style={{
                        background: D.surfaceAlt,
                        border: `1px solid ${D.border}`,
                        color: D.text1,
                        lineHeight: 1.55,
                      }}
                    >
                      {visible.length > 0 ? visible.join("\n") : t("无堆栈信息")}
                    </pre>
                    {hasMore && (
                      <button
                        onClick={() => setStackExpanded((v) => !v)}
                        className="mt-2 text-xs font-medium"
                        style={{ color: D.accent, background: "none", border: "none", cursor: "pointer", padding: 0 }}
                      >
                        {stackExpanded
                          ? `▲ ${t("收起堆栈")}`
                          : `▼ ${t("查看更多")}（${t("共")} ${lines.length} ${t("行")}，${t("已显示")} ${PREVIEW} ${t("行")}）`}
                      </button>
                    )}
                  </div>
                );
              })()}
            </Section>

            <Section
              title={t("AI 分析（根因 / 修复方案）")}
              right={(detail.analysis && (detail.analysis as any).agent_model) ? (
                <span
                  className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-mono"
                  style={{
                    background: "rgba(124,58,237,0.10)",
                    color: "#7C3AED",
                    border: "1px solid rgba(124,58,237,0.25)",
                  }}
                  title={t("分析使用的模型")}
                >
                  🤖 {(detail.analysis as any).agent_model}
                </span>
              ) : null}
            >
              {detail.analysis && "root_cause" in detail.analysis && (detail.analysis as any).root_cause ? (
                <div className="space-y-3 text-xs">
                  {(detail.analysis as any).scenario && (
                    <div>
                      <div className="text-[11px] uppercase tracking-wider mb-1.5" style={{ color: D.text3 }}>
                        {t("场景")}
                      </div>
                      <div className="text-[12.5px] leading-relaxed crashguard-md" style={{ color: D.text1 }}>
                        <MarkdownText>{(detail.analysis as any).scenario}</MarkdownText>
                      </div>
                    </div>
                  )}

                  {/* 多根因列表 — 按置信度分色卡片 */}
                  {Array.isArray((detail.analysis as any).possible_causes) && (detail.analysis as any).possible_causes.length > 0 && (
                    <div>
                      <div className="flex items-center justify-between mb-3">
                        <div className="text-[11px] uppercase tracking-wider font-semibold" style={{ color: D.text3 }}>
                          {t("可能原因（按可信度排序）")}
                        </div>
                        <span className="text-[10px]" style={{ color: D.text3 }}>
                          {((detail.analysis as any).possible_causes as any[]).length} {t("条")}
                        </span>
                      </div>
                      <div className="space-y-3">
                        {((detail.analysis as any).possible_causes as any[]).map((c, idx) => {
                          const conf = (c.confidence || "").toLowerCase();
                          const isHigh = conf === "high";
                          const isMid = conf === "medium";
                          const accentColor = isHigh ? "#DC2626" : isMid ? "#D97706" : "#5B6470";
                          const tintBg = isHigh
                            ? "linear-gradient(135deg, rgba(239,68,68,0.06), rgba(239,68,68,0.02))"
                            : isMid
                            ? "linear-gradient(135deg, rgba(245,158,11,0.06), rgba(245,158,11,0.02))"
                            : D.surfaceAlt;
                          const borderL = `4px solid ${accentColor}`;
                          return (
                            <div
                              key={idx}
                              className="rounded-md overflow-hidden"
                              style={{
                                background: tintBg,
                                border: `1px solid ${D.border}`,
                                borderLeft: borderL,
                              }}
                            >
                              {/* 头部：序号 + 标题 + 置信度 */}
                              <div className="flex items-start gap-3 px-3.5 py-2.5">
                                <div
                                  className="flex-shrink-0 rounded-full flex items-center justify-center font-bold"
                                  style={{
                                    width: 28,
                                    height: 28,
                                    background: accentColor,
                                    color: "#FFFFFF",
                                    fontSize: 12,
                                  }}
                                >
                                  {idx + 1}
                                </div>
                                <div className="flex-1 min-w-0">
                                  <div className="flex items-center gap-2 flex-wrap">
                                    <span className="font-semibold text-[13px] leading-tight" style={{ color: D.text1 }}>
                                      {c.title || "—"}
                                    </span>
                                    <span
                                      className="inline-flex rounded-full px-2 py-0.5 text-[9px] font-bold uppercase tracking-wide"
                                      style={{
                                        background: accentColor,
                                        color: "#FFFFFF",
                                      }}
                                    >
                                      {conf || "?"}
                                    </span>
                                  </div>
                                </div>
                              </div>
                              {/* code pointer 醒目代码块 */}
                              {c.code_pointer && (
                                <div
                                  className="px-3.5 py-1.5 text-[11px] font-mono"
                                  style={{
                                    background: "#0F172A",
                                    color: "#7DD3FC",
                                    borderTop: `1px solid ${D.border}`,
                                  }}
                                >
                                  📍 {c.code_pointer}
                                </div>
                              )}
                              {/* 证据 */}
                              {c.evidence && (
                                <div
                                  className="px-3.5 py-2 text-[11.5px] leading-relaxed"
                                  style={{
                                    color: D.text2,
                                    borderTop: `1px solid ${D.border}`,
                                    background: D.surface,
                                  }}
                                >
                                  <span className="font-semibold" style={{ color: D.text3 }}>
                                    {t("证据")}：
                                  </span>{" "}
                                  {c.evidence}
                                </div>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  )}

                  {/* 复杂度 + 解决方案/排查思路 */}
                  {(detail.analysis as any).complexity_kind && (() => {
                    const isSimple = (detail.analysis as any).complexity_kind === "simple";
                    const accent = isSimple ? "#059669" : "#D97706";
                    const tintBg = isSimple
                      ? "linear-gradient(135deg, rgba(5,150,105,0.06), rgba(5,150,105,0.02))"
                      : "linear-gradient(135deg, rgba(217,119,6,0.06), rgba(217,119,6,0.02))";
                    return (
                      <div
                        className="rounded-md overflow-hidden"
                        style={{
                          background: tintBg,
                          border: `1px solid ${D.border}`,
                          borderLeft: `4px solid ${accent}`,
                        }}
                      >
                        <div className="flex items-center gap-2 px-3.5 py-2.5"
                          style={{ borderBottom: `1px solid ${D.border}` }}>
                          <span style={{ fontSize: 18 }}>{isSimple ? "🛠️" : "🧭"}</span>
                          <div className="flex-1">
                            <div className="text-[13px] font-semibold" style={{ color: D.text1 }}>
                              {isSimple ? t("修复方案（可直接采用）") : t("排查思路（需开发者跟进）")}
                            </div>
                            <div className="text-[10.5px] mt-0.5" style={{ color: D.text3 }}>
                              {isSimple ? t("AI 评估为单点可修复，含可执行 patch") : t("AI 评估为跨模块/竞态/多假设，给出排查方向")}
                            </div>
                          </div>
                          <span
                            className="inline-flex rounded-full px-2 py-0.5 text-[10px] font-bold uppercase"
                            style={{ background: accent, color: "#FFFFFF" }}
                          >
                            {isSimple ? t("简单") : t("复杂")}
                          </span>
                        </div>
                        <div
                          className="px-3.5 py-2.5 text-[12.5px] leading-relaxed crashguard-md"
                          style={{ color: D.text1, background: D.surface }}
                        >
                          <MarkdownText>
                            {isSimple
                              ? ((detail.analysis as any).solution || (detail.analysis as any).fix_suggestion || "—")
                              : ((detail.analysis as any).hint || (detail.analysis as any).fix_suggestion || "—")}
                          </MarkdownText>
                        </div>
                      </div>
                    );
                  })()}

                  {/* 兜底：旧版无 possible_causes 时仍展示 root_cause + fix_suggestion */}
                  {(!Array.isArray((detail.analysis as any).possible_causes) || (detail.analysis as any).possible_causes.length === 0) && (
                    <>
                      <div>
                        <div className="text-[11px] uppercase tracking-wider mb-1.5" style={{ color: D.text3 }}>{t("根因")}</div>
                        <div className="text-[12.5px] leading-relaxed crashguard-md" style={{ color: D.text1 }}>
                          <MarkdownText>{(detail.analysis as any).root_cause || "—"}</MarkdownText>
                        </div>
                      </div>
                      <div>
                        <div className="text-[11px] uppercase tracking-wider mb-1.5" style={{ color: D.text3 }}>{t("修复建议")}</div>
                        <div className="text-[12.5px] leading-relaxed crashguard-md" style={{ color: D.text1 }}>
                          <MarkdownText>{(detail.analysis as any).fix_suggestion || "—"}</MarkdownText>
                        </div>
                      </div>
                    </>
                  )}

                  <div className="flex items-center gap-3 pt-1 flex-wrap">
                    <KV
                      k={t("可行度")}
                      v={`${(((detail.analysis as any).feasibility_score || 0) * 100).toFixed(0)}%`}
                    />
                    <KV k={t("置信度")} v={(detail.analysis as any).confidence || "—"} />
                  </div>
                </div>
              ) : (
                <div
                  className="rounded p-3 text-xs"
                  style={{
                    background: D.accentBg,
                    border: `1px dashed ${D.accent}`,
                    color: D.text2,
                  }}
                >
                  <div className="mb-2">
                    {t("尚未分析过该 issue。点击下方按钮触发 AI 根因分析（30-90 秒）。")}
                  </div>
                  <button
                    onClick={onAnalyze}
                    disabled={analyzing}
                    className="rounded px-3 py-1.5 text-xs font-semibold"
                    style={{
                      background: D.accent,
                      color: "#FFFFFF",
                      border: "none",
                      opacity: analyzing ? 0.5 : 1,
                    }}
                  >
                    {analyzing ? t("分析中...") : `🤖 ${t("开始分析")}`}
                  </button>
                </div>
              )}
            </Section>

            {/* 分析历史：每次分析（含 user prompt 引导）作为独立卡片，时间倒序 */}
            {analyses && analyses.length > 0 && (
              <Section title={`${t("分析历史")} (${analyses.length})`}>
                <div className="space-y-3">
                  {[...analyses].sort((a, b) => (b.created_at || "").localeCompare(a.created_at || "")).map((a) => {
                    const hasPrompt = Boolean((a.followup_question || "").trim());
                    const isFollowup = Boolean(a.is_followup);
                    const headerBg = hasPrompt ? "rgba(167,139,250,0.08)" : "rgba(14,124,134,0.06)";
                    const headerColor = hasPrompt ? "#7C3AED" : D.accent;
                    const headerIcon = hasPrompt ? "💬" : "🔍";
                    const headerLabel = hasPrompt
                      ? `${t("引导 prompt")}`
                      : `${t("默认分析")}`;
                    return (
                      <div key={a.run_id}
                        className="rounded-md overflow-hidden"
                        style={{ border: `1px solid ${D.border}` }}>
                        <div className="px-3 py-2 text-[12px] font-medium flex items-start gap-2"
                          style={{ background: headerBg, color: headerColor,
                                  borderBottom: `1px solid ${D.border}` }}>
                          <span style={{ flexShrink: 0 }}>{headerIcon}</span>
                          <div style={{ flex: 1, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
                            <span style={{ fontWeight: 600 }}>{headerLabel}：</span>
                            <span>{hasPrompt ? a.followup_question : t("未指定方向，跑全量根因分析")}</span>
                          </div>
                        </div>
                        <div className="px-3 py-2.5 text-[12.5px] leading-relaxed crashguard-md"
                          style={{ color: D.text1 }}>
                          {a.status === "success" ? (
                            isFollowup && a.answer ? (
                              <MarkdownText>{a.answer}</MarkdownText>
                            ) : (
                              <div className="space-y-2">
                                {a.root_cause && (
                                  <div>
                                    <div className="text-[11px] uppercase tracking-wider mb-0.5"
                                      style={{ color: D.text3 }}>{t("根因")}</div>
                                    <MarkdownText>{a.root_cause}</MarkdownText>
                                  </div>
                                )}
                                {a.fix_suggestion && (
                                  <div>
                                    <div className="text-[11px] uppercase tracking-wider mb-0.5 mt-1"
                                      style={{ color: D.text3 }}>{t("修复建议")}</div>
                                    <MarkdownText>{a.fix_suggestion}</MarkdownText>
                                  </div>
                                )}
                                {!a.root_cause && !a.fix_suggestion && (
                                  <span style={{ color: D.text3 }}>—</span>
                                )}
                              </div>
                            )
                          ) : a.status === "running" || a.status === "pending" ? (
                            <span style={{ color: D.text2 }}>⏳ {t("AI 正在思考...")}</span>
                          ) : a.status === "failed" ? (
                            <span style={{ color: D.danger }}>❌ {a.error || t("失败")}</span>
                          ) : (
                            <span style={{ color: D.text3 }}>—</span>
                          )}
                        </div>
                        <div className="px-3 pb-2 text-[10px] font-mono flex items-center gap-3 flex-wrap"
                          style={{ color: D.text3 }}>
                          {a.created_at && (
                            <span>{a.created_at.replace("T", " ").slice(0, 16)}</span>
                          )}
                          <span>🤖 {a.agent_model || a.agent_name || "agent"}</span>
                          {typeof a.feasibility_score === "number" && a.feasibility_score > 0 && (
                            <span>{t("可行度")} {(a.feasibility_score * 100).toFixed(0)}%</span>
                          )}
                          {a.confidence && <span>{t("置信度")} {a.confidence}</span>}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </Section>
            )}

            {/* 追问输入框（仅在已有 success 分析时显示） */}
            {analyses && analyses.some((a) => a.status === "success" && !a.is_followup) && (
              <section className="rounded-lg p-3"
                style={{ background: D.surfaceAlt, border: `1px solid ${D.border}` }}>
                <div className="text-[11px] uppercase tracking-wider mb-2"
                  style={{ color: D.text3 }}>
                  {t("追问 AI（基于上方分析继续问）")}
                </div>
                <div className="flex gap-2 items-end">
                  <textarea
                    value={followupText}
                    onChange={(e) => onFollowupChange(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                        onFollowupSubmit();
                      }
                    }}
                    placeholder={followupSubmitting ? t("AI 思考中，稍候...") : t("继续追问（⌘/Ctrl+Enter 发送）")}
                    rows={2}
                    disabled={followupSubmitting}
                    className="flex-1 resize-none rounded-lg px-3 py-2 text-[12px] outline-none"
                    style={{
                      background: D.surface,
                      border: `1px solid ${D.borderStrong}`,
                      color: D.text1,
                      minHeight: "40px",
                      maxHeight: "120px",
                    }}
                  />
                  <button
                    onClick={onFollowupSubmit}
                    disabled={!followupText.trim() || followupSubmitting}
                    className="flex-shrink-0 rounded-lg px-3 py-2 text-xs font-semibold"
                    style={{
                      background: "#7C3AED",
                      color: "#FFFFFF",
                      opacity: !followupText.trim() || followupSubmitting ? 0.5 : 1,
                    }}
                  >
                    {followupSubmitting ? `⏳` : `🚀`}
                  </button>
                </div>
              </section>
            )}

            {/* 深度诊断区块 */}
            <div style={{ marginTop: 24, borderTop: "1px solid #E5E7EB", paddingTop: 16 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
                <span style={{ fontWeight: 600, fontSize: 14 }}>🔍 深度诊断</span>
                {!diagStatus && !diagLoading && (
                  <button
                    onClick={() => onStartDeepAnalysis(detail.datadog_issue_id || "")}
                    style={{
                      padding: "4px 12px", fontSize: 12, borderRadius: 6,
                      background: "#1D4ED8", color: "#fff", border: "none", cursor: "pointer",
                    }}
                  >
                    启动（15-30 分钟）
                  </button>
                )}
                {diagLoading && <span style={{ fontSize: 12, color: "#5B6470" }}>启动中…</span>}
              </div>

              {diagStatus && (diagStatus.status === "pending" || diagStatus.status === "running") && (
                <div style={{ fontSize: 12, color: "#5B6470" }}>
                  ⏳ AI 正在调查中… 每 8 秒自动刷新
                  {(diagStatus.investigation_log?.length ?? 0) > 0 && (
                    <details style={{ marginTop: 6 }}>
                      <summary style={{ cursor: "pointer" }}>调查日志（{diagStatus.investigation_log.length} 步）</summary>
                      <ul style={{ paddingLeft: 16, marginTop: 4 }}>
                        {diagStatus.investigation_log.map((s, i) => (
                          <li key={i} style={{ marginBottom: 2 }}>{s}</li>
                        ))}
                      </ul>
                    </details>
                  )}
                </div>
              )}

              {diagStatus?.status === "success" && (
                <div>
                  <div style={{ fontSize: 12, color: "#5B6470", marginBottom: 8 }}>
                    总体置信度: {((diagStatus.overall_confidence ?? 0) * 100).toFixed(0)}% · 类型: {diagStatus.crash_type}
                  </div>
                  {diagStatus.hypotheses.map((h) => (
                    <div
                      key={h.id}
                      style={{
                        border: `1px solid ${h.id === diagStatus.recommended_hypothesis ? "#1D4ED8" : "#E5E7EB"}`,
                        borderRadius: 8, padding: "10px 14px", marginBottom: 10,
                        background: h.id === diagStatus.recommended_hypothesis ? "#EFF6FF" : "#F9FAFB",
                      }}
                    >
                      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
                        <div>
                          <span style={{ fontWeight: 600, fontSize: 13 }}>{h.title}</span>
                          {h.id === diagStatus.recommended_hypothesis && (
                            <span style={{
                              marginLeft: 6, fontSize: 10, background: "#1D4ED8", color: "#fff",
                              padding: "1px 6px", borderRadius: 4,
                            }}>推荐</span>
                          )}
                        </div>
                        <span style={{ fontSize: 12, color: "#5B6470" }}>{((h.confidence ?? 0) * 100).toFixed(0)}%</span>
                      </div>
                      <div style={{ fontSize: 11, color: "#5B6470", margin: "4px 0" }}>
                        {h.fix_direction}
                      </div>
                      <div style={{ fontSize: 11, color: "#374151", marginBottom: 6 }}>
                        {(h.evidence ?? []).slice(0, 3).map((ev, i) => (
                          <div key={i}>• {ev}</div>
                        ))}
                      </div>
                      <button
                        onClick={() => onConfirmHypothesis(diagStatus.run_id, h.id, detail.datadog_issue_id || "")}
                        disabled={diagConfirming === h.id}
                        style={{
                          fontSize: 11, padding: "3px 10px", borderRadius: 5,
                          background: "#16A34A", color: "#fff", border: "none", cursor: "pointer",
                          opacity: diagConfirming === h.id ? 0.6 : 1,
                        }}
                      >
                        {diagConfirming === h.id ? "触发中…" : "✓ 确认此假设 → 生成修复 PR"}
                      </button>
                    </div>
                  ))}

                  {(diagStatus.data_gaps?.length ?? 0) > 0 && (
                    <div style={{ marginTop: 8, padding: "8px 12px", background: "#FEF3C7", borderRadius: 6 }}>
                      <span style={{ fontSize: 12, fontWeight: 600 }}>⚠️ 数据缺口（需要更多监控数据）</span>
                      {diagStatus.data_gaps.map((gap, i) => (
                        <div key={i} style={{ fontSize: 11, marginTop: 4 }}>
                          <div>• {gap.description}</div>
                          {gap.collection_method && (
                            <div style={{ color: "#5B6470" }}>采集方式：{gap.collection_method}</div>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {diagStatus?.status === "failed" && (
                <div style={{ fontSize: 12, color: "#DC2626" }}>
                  诊断失败: {diagStatus.error || "未知错误"}
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// "Android 14 (40%), Android 13 (20%)" → [{label, pct}]
function parseDistribution(text: string): { label: string; pct: number }[] {
  if (!text) return [];
  const out: { label: string; pct: number }[] = [];
  for (const part of text.split(",")) {
    const m = part.trim().match(/^(.+?)\s*\(([\d.]+)\s*%\)\s*$/);
    if (m) out.push({ label: m[1].trim(), pct: parseFloat(m[2]) });
  }
  return out;
}

const PIE_PALETTE = ["#0E7C86", "#7C3AED", "#16A34A", "#DC2626", "#2563EB", "#D97706", "#9CA3AF"];

function PieChart({ title, slices }: { title: string; slices: { label: string; pct: number }[] }) {
  const size = 120;
  const cx = size / 2;
  const cy = size / 2;
  const r = size / 2 - 4;
  const total = slices.reduce((a, s) => a + s.pct, 0);
  // 凑满 100%（剩余 → "其他"）
  const display = [...slices];
  if (total > 0 && total < 99.5) {
    display.push({ label: "其他", pct: Math.max(0, 100 - total) });
  }
  let cum = 0;
  const paths = display.map((s, i) => {
    const startAngle = (cum / 100) * 2 * Math.PI - Math.PI / 2;
    cum += s.pct;
    const endAngle = (cum / 100) * 2 * Math.PI - Math.PI / 2;
    const x1 = cx + r * Math.cos(startAngle);
    const y1 = cy + r * Math.sin(startAngle);
    const x2 = cx + r * Math.cos(endAngle);
    const y2 = cy + r * Math.sin(endAngle);
    const largeArc = s.pct > 50 ? 1 : 0;
    // 单切片占 100% 时画整圆
    const d =
      display.length === 1
        ? `M ${cx - r} ${cy} A ${r} ${r} 0 1 1 ${cx + r} ${cy} A ${r} ${r} 0 1 1 ${cx - r} ${cy} Z`
        : `M ${cx} ${cy} L ${x1} ${y1} A ${r} ${r} 0 ${largeArc} 1 ${x2} ${y2} Z`;
    return { d, color: PIE_PALETTE[i % PIE_PALETTE.length], label: s.label, pct: s.pct };
  });
  return (
    <div className="rounded-md p-3" style={{ background: D.surfaceAlt, border: `1px solid ${D.border}` }}>
      <div className="text-[11px] font-medium mb-2" style={{ color: D.text2 }}>
        {title}
      </div>
      <div className="flex items-center gap-3">
        <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="flex-shrink-0">
          {paths.map((p, i) => (
            <path key={i} d={p.d} fill={p.color} stroke="#FFFFFF" strokeWidth="1.5" />
          ))}
        </svg>
        <ul className="flex-1 space-y-0.5 text-[10.5px]" style={{ color: D.text1 }}>
          {paths.map((p, i) => (
            <li key={i} className="flex items-center gap-1.5 truncate" title={`${p.label} ${p.pct.toFixed(1)}%`}>
              <span
                className="inline-block flex-shrink-0 rounded-sm"
                style={{ width: 8, height: 8, background: p.color }}
              />
              <span className="truncate">{p.label}</span>
              <span className="ml-auto tabular-nums" style={{ color: D.text2 }}>
                {p.pct.toFixed(1)}%
              </span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

const VERSION_PIE_PALETTE = ["#2563EB", "#16A34A", "#0E7C86", "#7C3AED", "#DC2626", "#D97706", "#9CA3AF", "#0891B2", "#DB2777", "#65A30D"];

function VersionPieCard({
  title,
  slices,
  color,
}: {
  title: string;
  slices: { version: string; sessions: number; pct: number }[];
  color: string;
}) {
  const t = useT();
  const size = 72;
  const cx = size / 2;
  const cy = size / 2;
  const r = size / 2 - 2;

  if (!slices.length) {
    return (
      <div className="rounded-lg p-4 flex flex-col gap-1" style={{ background: D.surface, border: `1px solid ${D.border}` }}>
        <div className="text-xs font-semibold" style={{ color }}>{title}</div>
        <div className="text-xs" style={{ color: D.text3 }}>暂无数据</div>
      </div>
    );
  }

  const total = slices.reduce((a, s) => a + s.pct, 0);
  const display = [...slices];
  if (total < 99.5) display.push({ version: "其他", sessions: 0, pct: Math.max(0, 100 - total) });

  let cum = 0;
  const paths = display.map((s, i) => {
    const startAngle = (cum / 100) * 2 * Math.PI - Math.PI / 2;
    cum += s.pct;
    const endAngle = (cum / 100) * 2 * Math.PI - Math.PI / 2;
    const x1 = cx + r * Math.cos(startAngle);
    const y1 = cy + r * Math.sin(startAngle);
    const x2 = cx + r * Math.cos(endAngle);
    const y2 = cy + r * Math.sin(endAngle);
    const largeArc = s.pct > 50 ? 1 : 0;
    const d =
      display.length === 1
        ? `M ${cx - r} ${cy} A ${r} ${r} 0 1 1 ${cx + r} ${cy} A ${r} ${r} 0 1 1 ${cx - r} ${cy} Z`
        : `M ${cx} ${cy} L ${x1} ${y1} A ${r} ${r} 0 ${largeArc} 1 ${x2} ${y2} Z`;
    return { d, c: VERSION_PIE_PALETTE[i % VERSION_PIE_PALETTE.length], label: s.version, pct: s.pct, sessions: s.sessions };
  });

  // 只展示 top5 in legend，其余折叠
  const legendItems = paths.slice(0, 5);

  return (
    <div className="rounded-lg p-4" style={{ background: D.surface, border: `1px solid ${D.border}` }}>
      <div className="text-xs font-semibold mb-3" style={{ color }}>{title} {t("版本分布")}</div>
      <div className="flex items-center gap-3">
        <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="flex-shrink-0">
          {paths.map((p, i) => (
            <path key={i} d={p.d} fill={p.c} stroke="#FFFFFF" strokeWidth="1.5" />
          ))}
        </svg>
        <ul className="flex-1 space-y-1 min-w-0">
          {legendItems.map((p, i) => (
            <li key={i} className="flex items-center gap-1.5 text-[10.5px] truncate" title={`${p.label} ${p.pct.toFixed(1)}%`}>
              <span className="flex-shrink-0 rounded-sm" style={{ width: 8, height: 8, background: p.c, display: "inline-block" }} />
              <span className="truncate" style={{ color: D.text1 }}>{p.label}</span>
              <span className="ml-auto tabular-nums flex-shrink-0" style={{ color: D.text2 }}>{p.pct.toFixed(1)}%</span>
            </li>
          ))}
          {paths.length > 5 && (
            <li className="text-[10px]" style={{ color: D.text3 }}>+{paths.length - 5} 个版本</li>
          )}
        </ul>
      </div>
    </div>
  );
}

// 紧凑饼图 + 右侧 legend（复用版本/OS 版本两处）
function MiniPie({
  slices,
  size = 56,
  palette,
}: {
  slices: { label: string; pct: number }[];
  size?: number;
  palette: string[];
}) {
  const cx = size / 2;
  const cy = size / 2;
  const r = size / 2 - 1;
  if (!slices.length) {
    return <div style={{ width: size, height: size, color: D.text3 }} className="flex items-center justify-center text-[10px]">—</div>;
  }
  const total = slices.reduce((a, s) => a + s.pct, 0);
  const display = [...slices];
  if (total < 99.5) display.push({ label: "其他", pct: Math.max(0, 100 - total) });
  let cum = 0;
  const paths = display.map((s, i) => {
    const startAngle = (cum / 100) * 2 * Math.PI - Math.PI / 2;
    cum += s.pct;
    const endAngle = (cum / 100) * 2 * Math.PI - Math.PI / 2;
    const x1 = cx + r * Math.cos(startAngle);
    const y1 = cy + r * Math.sin(startAngle);
    const x2 = cx + r * Math.cos(endAngle);
    const y2 = cy + r * Math.sin(endAngle);
    const largeArc = s.pct > 50 ? 1 : 0;
    const d =
      display.length === 1
        ? `M ${cx - r} ${cy} A ${r} ${r} 0 1 1 ${cx + r} ${cy} A ${r} ${r} 0 1 1 ${cx - r} ${cy} Z`
        : `M ${cx} ${cy} L ${x1} ${y1} A ${r} ${r} 0 ${largeArc} 1 ${x2} ${y2} Z`;
    return { d, c: palette[i % palette.length] };
  });
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="flex-shrink-0">
      {paths.map((p, i) => (
        <path key={i} d={p.d} fill={p.c} stroke="#FFFFFF" strokeWidth="1" />
      ))}
    </svg>
  );
}

function PieLegend({
  items,
  palette,
  maxRows = 6,
}: {
  items: { label: string; pct: number; sessions?: number }[];
  palette: string[];
  maxRows?: number;
}) {
  const top = items.slice(0, maxRows);
  return (
    <ul className="flex-1 space-y-1 min-w-0">
      {top.map((p, i) => (
        <li
          key={i}
          className="flex items-center gap-1.5 text-[11px]"
          title={`${p.label} · ${p.pct.toFixed(1)}%${p.sessions != null ? ` · ${p.sessions.toLocaleString()} sessions` : ""}`}
        >
          <span className="flex-shrink-0 rounded-sm" style={{ width: 8, height: 8, background: palette[i % palette.length] }} />
          <span className="truncate flex-1" style={{ color: D.text1 }}>{p.label}</span>
          <span className="tabular-nums flex-shrink-0" style={{ color: D.text1, minWidth: 38, textAlign: "right" }}>
            {p.pct.toFixed(1)}%
          </span>
          {p.sessions != null && (
            <span className="tabular-nums flex-shrink-0" style={{ color: D.text3, minWidth: 40, textAlign: "right" }}>
              {compactNumber(p.sessions)}
            </span>
          )}
        </li>
      ))}
      {items.length > maxRows && (
        <li className="text-[10px]" style={{ color: D.text3 }}>+{items.length - maxRows}</li>
      )}
    </ul>
  );
}

// KPI 顶栏单元（方案 C）
function KpiStripCell({
  label,
  value,
  hint,
  accent,
  active,
  onClick,
}: {
  label: string;
  value: string;
  hint: string;
  accent: string;
  active?: boolean;
  onClick?: () => void;
}) {
  const interactive = !!onClick;
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={!interactive}
      className={`text-left rounded-lg px-3 py-2.5 transition ${interactive ? "cursor-pointer" : "cursor-default"}`}
      style={{
        background: D.surface,
        border: `1px solid ${active ? accent : D.border}`,
        boxShadow: active ? `0 0 0 2px ${accent}33` : "none",
      }}
    >
      <div className="text-[10px] uppercase tracking-wider" style={{ color: D.text3 }}>
        {label}
      </div>
      <div className="text-xl font-bold mt-0.5" style={{ color: accent }}>
        {value}
      </div>
      <div className="text-[10px] mt-0.5 truncate" style={{ color: D.text2 }}>
        {hint}
      </div>
    </button>
  );
}

const OS_PIE_PALETTE = [
  "#06B6D4", "#0EA5E9", "#3B82F6", "#6366F1", "#8B5CF6",
  "#A855F7", "#D946EF", "#EC4899",
];

// 解析 "3.18.0-708" → [3,18,0]；失败返回 null
// QA filter 口径与 backend pipeline._is_qa_version 对齐（patch >= 100 视为内测包）
function _parseProdSemver(version: string): [number, number, number] | null {
  try {
    const stem = (version || "").split("-")[0];
    const parts = stem.split(".");
    if (parts.length < 3) return null;
    const major = parseInt(parts[0], 10);
    const minor = parseInt(parts[1], 10);
    const patch = parseInt(parts[2], 10);
    if (Number.isNaN(major) || Number.isNaN(minor) || Number.isNaN(patch)) return null;
    return [major, minor, patch];
  } catch {
    return null;
  }
}

// 从版本分布里挑「线上最新版本」：
//   - patch < 100（线上版本；patch >= 100 是 QA 内测包，与后端 pipeline._is_qa_version 同口径）
//   - sessions > 100（统计显著性门槛，避免冷启动 / 灰度数据扰动）
//   - 满足上述过滤后取 semver 最大者
// 全部不满足返回 undefined（前端兼容降级，不显示 latest 行）
function _pickLatestRelease(
  versions: CrashVersionSlice[],
  minSessions = 100,
  qaPatchThreshold = 100,
): CrashVersionSlice | undefined {
  const qualified = versions
    .map((v) => ({ slice: v, sv: _parseProdSemver(v.version) }))
    .filter(
      (x) =>
        x.sv !== null &&
        x.sv[2] < qaPatchThreshold &&
        (x.slice.sessions ?? 0) > minSessions,
    );
  if (qualified.length === 0) return undefined;
  qualified.sort((a, b) => {
    const [aM, am, ap] = a.sv as [number, number, number];
    const [bM, bm, bp] = b.sv as [number, number, number];
    if (aM !== bM) return bM - aM;
    if (am !== bm) return bm - am;
    return bp - ap;
  });
  return qualified[0].slice;
}

// 平台总览卡（方案 D：iOS / Android 各一张）
function PlatformOverviewCard({
  label,
  accent,
  mainVersion,
  mainVersionPct,
  versions,
  osVersions,
  summary,
}: {
  label: string;
  accent: string;
  mainVersion?: string;
  mainVersionPct?: number;
  versions: CrashVersionSlice[];
  osVersions: CrashOsVersionSlice[];
  summary?: CrashPlatformSummary;
}) {
  const t = useT();
  const verSlices = versions.map((v) => ({ label: v.version, pct: v.pct, sessions: v.sessions }));
  const osSlices = osVersions.map((v) => ({ label: v.version, pct: v.pct, sessions: v.sessions }));
  const cfPct = summary?.crash_free_pct;
  const cfColor =
    cfPct == null ? D.text3 : cfPct >= 99.5 ? D.ok : cfPct >= 98 ? D.warn : D.danger;
  // 主版本对应的 slice（用于补充 sessions/crashes 颗粒度）
  // mainVersionPct 是 versions[0].pct 派生（见调用点），mainSlice 同源
  const mainSlice = versions[0];
  const mainSessions = mainSlice?.sessions;
  const mainCrashes = mainSlice?.crashes;
  // 线上最新版本（filter 后取最大 semver；可能与 mainVersion 相同也可能不同）
  const latestSlice = _pickLatestRelease(versions);
  const latestSameAsMain = !!(latestSlice && mainVersion && latestSlice.version === mainVersion);
  // mainVersionPct 是百分比，告诉用户「86.1% 是占 sessions 的比例」需要绝对数兜底
  // tooltip 把含义和样本都说清楚
  const mainTitle = [
    mainVersion || "",
    mainVersionPct != null ? `${mainVersionPct.toFixed(1)}% sessions` : "",
    mainSessions != null ? `${mainSessions.toLocaleString()} sessions` : "",
    mainCrashes != null ? `${mainCrashes.toLocaleString()} crashed sessions` : "",
  ].filter(Boolean).join(" · ");
  const latestTitle = latestSlice
    ? [
        latestSlice.version,
        `${latestSlice.sessions.toLocaleString()} sessions`,
        latestSlice.crashes != null ? `${latestSlice.crashes.toLocaleString()} crashed sessions` : "",
        `patch < 100 & sessions > 100`,
      ].filter(Boolean).join(" · ")
    : "";
  return (
    <div className="rounded-lg p-4" style={{ background: D.surface, border: `1px solid ${D.border}` }}>
      {/* Header: platform + main version + 绝对数兜底（让 86.1% 不再误导） */}
      <div className="flex items-baseline justify-between mb-3 gap-2 flex-wrap">
        <div className="flex items-baseline gap-2 min-w-0 flex-wrap">
          <span className="text-base font-bold" style={{ color: accent }}>{label}</span>
          <span className="text-xs truncate" style={{ color: D.text2 }} title={mainTitle}>
            {t("主版本")} <strong style={{ color: D.text1 }}>{mainVersion || "—"}</strong>
            {mainVersionPct != null && (
              <span style={{ color: D.text3 }}> ({mainVersionPct.toFixed(1)}%)</span>
            )}
          </span>
          {(mainSessions != null || mainCrashes != null) && (
            <span className="text-[11px] tabular-nums" style={{ color: D.text3 }}>
              {mainSessions != null && (
                <>
                  <span style={{ color: D.text2 }}>{compactNumber(mainSessions)}</span>
                  {" sessions"}
                </>
              )}
              {mainSessions != null && mainCrashes != null && (
                <span style={{ color: D.text3 }}> · </span>
              )}
              {mainCrashes != null && (
                <>
                  <span style={{ color: D.danger }}>{compactNumber(mainCrashes)}</span>
                  {" crashes"}
                </>
              )}
            </span>
          )}
          {latestSameAsMain && (
            <span
              className="text-[10px] px-1.5 py-0.5 rounded"
              style={{ background: D.surfaceAlt, color: D.text3, border: `1px solid ${D.border}` }}
              title={t("主版本即线上最新版本（patch<100 & sessions>100）")}
            >
              = {t("最新版本")}
            </span>
          )}
        </div>
      </div>

      {/* 「线上最新版本」独立行：仅当与主版本不同（避免冗余） */}
      {latestSlice && !latestSameAsMain && (
        <div className="flex items-baseline gap-2 mb-3 -mt-1 flex-wrap">
          <span className="text-xs truncate" style={{ color: D.text2 }} title={latestTitle}>
            {t("最新版本")} <strong style={{ color: D.text1 }}>{latestSlice.version}</strong>
            <span style={{ color: D.text3 }}> ({latestSlice.pct.toFixed(1)}%)</span>
          </span>
          <span className="text-[11px] tabular-nums" style={{ color: D.text3 }}>
            <span style={{ color: D.text2 }}>{compactNumber(latestSlice.sessions)}</span>
            {" sessions"}
            {latestSlice.crashes != null && (
              <>
                <span style={{ color: D.text3 }}> · </span>
                <span style={{ color: D.danger }}>{compactNumber(latestSlice.crashes)}</span>
                {" crashes"}
              </>
            )}
          </span>
        </div>
      )}

      {/* Crash-free 概要：CF% / total / crashed sessions（突出 CF rate） */}
      <div
        className="grid grid-cols-3 gap-2 rounded-md px-3 py-2 mb-3"
        style={{ background: D.surfaceAlt, border: `1px solid ${D.border}` }}
      >
        <div>
          <div className="text-[9.5px] uppercase tracking-wider" style={{ color: D.text3 }}>Crash-free</div>
          <div className="text-lg font-bold tabular-nums" style={{ color: cfColor }}>
            {cfPct != null ? `${cfPct.toFixed(2)}%` : "—"}
          </div>
        </div>
        <div>
          <div className="text-[9.5px] uppercase tracking-wider" style={{ color: D.text3 }}>Sessions</div>
          <div className="text-lg font-bold tabular-nums" style={{ color: D.text1 }}>
            {summary?.total_sessions != null ? compactNumber(summary.total_sessions) : "—"}
          </div>
        </div>
        <div>
          <div className="text-[9.5px] uppercase tracking-wider" style={{ color: D.text3 }}>Crashed</div>
          <div className="text-lg font-bold tabular-nums" style={{ color: D.danger }}>
            {summary?.crashed_sessions != null ? compactNumber(summary.crashed_sessions) : "—"}
          </div>
        </div>
      </div>

      {/* 两个紧凑饼图：App 版本 + OS 版本 */}
      <div className="grid grid-cols-2 gap-4">
        <div>
          <div className="text-[10px] uppercase tracking-wider mb-2" style={{ color: D.text3 }}>
            {t("App 版本")}
          </div>
          <div className="flex items-center gap-3">
            <MiniPie slices={verSlices} palette={VERSION_PIE_PALETTE} size={104} />
            <PieLegend items={verSlices} palette={VERSION_PIE_PALETTE} />
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider mb-2" style={{ color: D.text3 }}>
            {t("OS 版本")}
          </div>
          <div className="flex items-center gap-3">
            <MiniPie slices={osSlices} palette={OS_PIE_PALETTE} size={104} />
            <PieLegend items={osSlices} palette={OS_PIE_PALETTE} />
          </div>
        </div>
      </div>
    </div>
  );
}

function Section({ title, children, right }: { title: string; children: React.ReactNode; right?: React.ReactNode }) {
  return (
    <div>
      <div
        className="flex items-center justify-between mb-2"
      >
        <div className="text-[11px] uppercase tracking-wider" style={{ color: D.text3 }}>
          {title}
        </div>
        {right || null}
      </div>
      <div className="space-y-1.5">{children}</div>
    </div>
  );
}

function KV({ k, v, multiline }: { k: string; v: string; multiline?: boolean }) {
  return (
    <div className={multiline ? "" : "flex items-baseline justify-between gap-3"}>
      <span className="text-xs" style={{ color: D.text3 }}>
        {k}
      </span>
      <span
        className={`text-xs ${multiline ? "block mt-1 whitespace-pre-wrap" : ""}`}
        style={{ color: D.text1 }}
      >
        {v}
      </span>
    </div>
  );
}
