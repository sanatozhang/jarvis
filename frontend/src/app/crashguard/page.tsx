"use client";

import { Suspense, useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams, usePathname } from "next/navigation";
import { useT } from "@/lib/i18n";
import { Toast } from "@/components/Toast";
import MarkdownText from "@/components/MarkdownText";
import {
  fetchCrashTop,
  fetchCrashIssue,
  fetchCrashHealth,
  triggerCrashPipeline,
  updateCrashIssue,
  analyzeCrashIssue,
  batchAnalyzeCrash,
  fetchCrashAnalyses,
  followupCrashIssue,
  fetchCrashAnalysisStatus,
  runCrashDailyReport,
  type CrashAnalysisRecord,
  type CrashTopItem,
  type CrashIssueDetail,
  type CrashStatus,
} from "@/lib/api";

// jarvis 主站浅色金调（Firebase-style 布局 + 主题对齐）
const D = {
  bg: "#F8F9FA",
  surface: "#FFFFFF",
  surfaceAlt: "#F8F9FA",
  border: "rgba(0,0,0,0.08)",
  borderStrong: "rgba(0,0,0,0.14)",
  text1: "#111827",
  text2: "#6B7280",
  text3: "#9CA3AF",
  accent: "#B8922E",                       // jarvis gold
  accentBg: "rgba(184,146,46,0.08)",
  ok: "#16A34A",
  warn: "#D97706",
  warnBg: "rgba(217,119,6,0.10)",
  danger: "#DC2626",
  dangerBg: "rgba(220,38,38,0.08)",
  p0: "#DC2626",
  p1: "#2563EB",
  hover: "#EEF0F2",
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
  ignored: { fg: "#6B7280", bg: "rgba(107,114,128,0.10)" },
  wontfix: { fg: "#6B7280", bg: "rgba(107,114,128,0.10)" },
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
    <Suspense fallback={<div style={{ padding: 32, color: "#6B7280" }}>加载中...</div>}>
      <CrashguardPageInner />
    </Suspense>
  );
}

function CrashguardPageInner() {
  const t = useT();
  const [items, setItems] = useState<CrashTopItem[]>([]);
  const [date, setDate] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [triggering, setTriggering] = useState(false);
  const [batching, setBatching] = useState(false);
  const [reportBusy, setReportBusy] = useState(false);
  const [reportModal, setReportModal] = useState<{
    title: string;
    preview: string;
    reportType: "morning" | "evening";
    error?: string;
  } | null>(null);
  const [platformFilter, setPlatformFilter] = useState<string>("all");
  const [tierFilter, setTierFilter] = useState<"all" | "P0" | "P1">("all");
  const [statusFilter, setStatusFilter] = useState<"all" | CrashStatus>("all");
  const [sortBy, setSortBy] = useState<"impact" | "events" | "users" | "new_first">("impact");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

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
  const [detail, setDetail] = useState<CrashIssueDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [analyses, setAnalyses] = useState<CrashAnalysisRecord[]>([]);
  const [followupText, setFollowupText] = useState("");
  const [followupSubmitting, setFollowupSubmitting] = useState(false);
  const [toast, setToast] = useState<{ msg: string; type: "success" | "error" } | null>(null);
  const [datadogConfigured, setDatadogConfigured] = useState<boolean | null>(null);
  const [savingPatch, setSavingPatch] = useState(false);
  const [search, setSearch] = useState("");
  const [analyzing, setAnalyzing] = useState(false);

  const platforms = useMemo(() => {
    const set = new Set<string>();
    items.forEach((i) => set.add((i.platform || "").toLowerCase()));
    return Array.from(set).filter(Boolean).sort();
  }, [items]);

  const filteredItems = useMemo(() => {
    const q = search.trim().toLowerCase();
    const filtered = items.filter((i) => {
      if (platformFilter !== "all" && (i.platform || "").toLowerCase() !== platformFilter) return false;
      if (tierFilter !== "all" && i.tier !== tierFilter) return false;
      if (statusFilter !== "all" && i.status !== statusFilter) return false;
      if (q && !(i.title.toLowerCase().includes(q) || i.datadog_issue_id.toLowerCase().includes(q))) return false;
      return true;
    });
    const sorted = [...filtered];
    sorted.sort((a, b) => {
      if (sortBy === "events") return (b.events_count || 0) - (a.events_count || 0);
      if (sortBy === "users") return (b.users_affected || 0) - (a.users_affected || 0);
      if (sortBy === "new_first") {
        const score = (x: typeof a) =>
          (x.is_new_in_version ? 100 : 0) + (x.is_regression ? 50 : 0) + (x.is_surge ? 25 : 0);
        const ds = score(b) - score(a);
        if (ds !== 0) return ds;
        return (b.crash_free_impact_score || 0) - (a.crash_free_impact_score || 0);
      }
      // impact (default)
      return (b.crash_free_impact_score || 0) - (a.crash_free_impact_score || 0);
    });
    return sorted;
  }, [items, platformFilter, tierFilter, statusFilter, search, sortBy]);

  const totals = useMemo(() => {
    const events = items.reduce((s, i) => s + (i.events_count || 0), 0);
    const sessions = items.reduce((s, i) => s + (i.sessions_affected || 0), 0);
    const p0 = items.filter((i) => i.tier === "P0").length;
    const surge = items.filter((i) => i.is_surge).length;
    return { events, sessions, p0, surge };
  }, [items]);

  const loadTop = async () => {
    setLoading(true);
    try {
      const [resp, h] = await Promise.all([fetchCrashTop(40), fetchCrashHealth()]);
      setItems(resp.issues);
      setDate(resp.date);
      setDatadogConfigured(h.datadog_configured);
    } catch (e: any) {
      setToast({ msg: e.message || "load failed", type: "error" });
    } finally {
      setLoading(false);
    }
  };

  const loadDetail = async (issueId: string) => {
    setDetailLoading(true);
    setSelectedId(issueId);
    setDetail(null);
    setAnalyses([]);
    setFollowupText("");
    syncSelectedToUrl(issueId);
    try {
      const [d, list] = await Promise.all([
        fetchCrashIssue(issueId),
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

  const onAnalyze = async (issueId: string) => {
    if (analyzing) return;
    setAnalyzing(true);
    setToast({ msg: t("AI 分析中，可能需要 30-90 秒..."), type: "success" });
    try {
      const res = await analyzeCrashIssue(issueId);
      if (res.status === "failed") {
        setToast({ msg: t("分析失败: ") + (res.error || "unknown"), type: "error" });
      } else {
        setToast({ msg: t("分析完成"), type: "success" });
      }
      if (selectedId === issueId) {
        const fresh = await fetchCrashIssue(issueId);
        setDetail(fresh);
      }
    } catch (e: any) {
      setToast({ msg: e.message || "analyze failed", type: "error" });
    } finally {
      setAnalyzing(false);
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
    }
  };

  const onSendReport = async () => {
    if (!reportModal) return;
    if (!confirm(t("确认发送到飞书群？"))) return;
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

  const onBatchAnalyze = async () => {
    if (!confirm(t("批量启动 AI 分析（仅未分析过的 Top N）。继续？"))) return;
    setBatching(true);
    try {
      const res = await batchAnalyzeCrash();
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

  const onTrigger = async () => {
    if (!confirm(t("立即拉取一次 Datadog（约 5 秒）？"))) return;
    setTriggering(true);
    try {
      const res = await triggerCrashPipeline("3.16.0", ["3.16.0", "3.15.5", "3.15.4", "3.15.3", "3.14.9", "3.14.8"]);
      setToast({
        msg: t("已拉取 ") + res.issues_processed + t(" 条，Top ") + res.top_n_count,
        type: "success",
      });
      await loadTop();
    } catch (e: any) {
      setToast({ msg: e.message || "trigger failed", type: "error" });
    } finally {
      setTriggering(false);
    }
  };

  useEffect(() => {
    loadTop();
  }, []);

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
        {/* Top filter bar (Firebase style) */}
        <div
          className="flex items-center justify-between gap-3 px-6 py-3"
          style={{ background: D.bg, borderBottom: `1px solid ${D.border}` }}
        >
          <div className="flex items-center gap-2 flex-wrap">
            <Pill icon="filter" label={t("筛选")} />
            <FilterPill
              label={t("平台")}
              value={platformFilter}
              onChange={setPlatformFilter}
              options={[{ v: "all", l: t("全部") }, ...platforms.map((p) => ({ v: p, l: platformLabel(p) }))]}
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
          </div>
          <div className="flex items-center gap-2 text-xs" style={{ color: D.text2 }}>
            <span>📅 {date || "—"}</span>
          </div>
        </div>

        {/* Latest release banner */}
        <div
          className="flex items-center justify-between px-6 py-3 mx-6 mt-4 rounded-lg"
          style={{ background: D.surface, border: `1px solid ${D.border}` }}
        >
          <div className="flex items-center gap-3">
            <span
              className="inline-flex items-center justify-center h-7 w-7 rounded-full text-xs"
              style={{ background: D.warnBg, color: D.warn }}
            >
              ⚠
            </span>
            <span className="text-sm" style={{ color: D.text1 }}>
              {t("最新版本")} <strong>3.16.0</strong>
            </span>
            <span className="text-sm" style={{ color: D.text2 }}>
              · {totals.sessions.toLocaleString()} {t("受影响会话")} · {totals.events.toLocaleString()} {t("总事件")}
            </span>
            {datadogConfigured === false && (
              <span className="text-sm" style={{ color: D.danger }}>
                · {t("Datadog 未配置")}
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={loadTop}
              disabled={loading}
              className="rounded px-3 py-1.5 text-xs font-medium"
              style={{
                background: "transparent",
                border: `1px solid ${D.borderStrong}`,
                color: D.text1,
                opacity: loading ? 0.5 : 1,
              }}
            >
              {loading ? t("加载中...") : t("刷新")}
            </button>
            <button
              onClick={onTrigger}
              disabled={triggering}
              className="rounded px-3 py-1.5 text-xs font-medium inline-flex items-center gap-1.5"
              style={{ background: D.accent, color: "#FFFFFF", opacity: triggering ? 0.5 : 1 }}
            >
              {triggering ? t("拉取中...") : t("立即拉取")} →
            </button>
            <button
              onClick={onBatchAnalyze}
              disabled={batching}
              className="rounded px-3 py-1.5 text-xs font-medium inline-flex items-center gap-1.5"
              style={{
                background: "transparent",
                border: `1px solid ${D.borderStrong}`,
                color: D.text1,
                opacity: batching ? 0.5 : 1,
              }}
              title={t("对今日 Top N（未分析过的）批量启动 AI 分析")}
            >
              {batching ? t("批量分析中...") : `🤖 ${t("批量分析 Top N")}`}
            </button>
            <button
              onClick={() => onPreviewReport("morning")}
              disabled={reportBusy}
              className="rounded px-3 py-1.5 text-xs font-medium inline-flex items-center gap-1.5"
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
              className="rounded px-3 py-1.5 text-xs font-medium inline-flex items-center gap-1.5"
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
          </div>
        </div>

        {/* Two-card stat row (Firebase Crash-free style) */}
        <div className="grid grid-cols-2 gap-3 px-6 mt-3">
          <StatCardLarge
            title={t("Crash-free 会话")}
            primary={"—"}
            secondary={`${totals.sessions.toLocaleString()} ${t("受影响会话")}`}
            hint={t("session-level，Datadog impacted_sessions")}
          />
          <StatCardLarge
            title={t("Crash-free 用户")}
            primary={"—"}
            secondary={t("Datadog Error Tracking 不返回 user 维度（Plan 2.5 接入 RUM Events API）")}
            hint=""
            muted
          />
        </div>

        {/* Trends mini-panel */}
        <div className="grid grid-cols-3 gap-3 px-6 mt-3">
          <TrendCard title={t("总崩溃 issue")} value={items.length.toString()} hint={t("Top 40 范围内")} />
          <TrendCard title="P0" value={totals.p0.toString()} hint={t("新增 / 回归 / 飙升")} accent={D.danger} />
          <TrendCard title={t("飙升")} value={totals.surge.toString()} hint={t("当日翻倍并 ≥ 10 events")} accent={D.warn} />
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
                  <th className="text-center px-3 py-2 font-medium" style={{ width: 50 }}>
                    DD
                  </th>
                </tr>
              </thead>
              <tbody>
                {filteredItems.length === 0 && !loading && (
                  <tr>
                    <td colSpan={7} className="px-6 py-12 text-center" style={{ color: D.text3 }}>
                      {t("暂无数据，先点【立即拉取】触发一次 Datadog 同步")}
                    </td>
                  </tr>
                )}
                {filteredItems.map((it, idx) => {
                  const active = selectedId === it.datadog_issue_id;
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
          onAnalyze={() => onAnalyze(selectedId!)}
          onPatch={(patch) => onPatch(selectedId!, patch)}
          onClose={() => {
            setSelectedId(null);
            setDetail(null);
            syncSelectedToUrl(null);
          }}
          t={t}
        />
      )}

      {toast && <Toast msg={toast.msg} type={toast.type} onClose={() => setToast(null)} />}

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
  t: (k: string) => string;
}) {
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
              <pre
                className="rounded p-3 text-xs font-mono overflow-auto whitespace-pre-wrap"
                style={{
                  background: D.surfaceAlt,
                  border: `1px solid ${D.border}`,
                  maxHeight: 320,
                  color: D.text1,
                }}
              >
                {detail.representative_stack || t("无堆栈信息")}
              </pre>
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
                          const accentColor = isHigh ? "#DC2626" : isMid ? "#D97706" : "#6B7280";
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

                  <div className="flex items-center gap-3 pt-1">
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

            {/* 追问会话 thread */}
            {analyses && analyses.filter((a) => a.is_followup).length > 0 && (
              <Section title={t("追问会话")}>
                <div className="space-y-3">
                  {analyses.filter((a) => a.is_followup).map((a) => (
                    <div key={a.run_id}
                      className="rounded-md overflow-hidden"
                      style={{ border: `1px solid ${D.border}` }}>
                      <div className="px-3 py-2 text-[12px] font-medium"
                        style={{ background: "rgba(167,139,250,0.08)", color: "#7C3AED",
                                borderBottom: `1px solid ${D.border}` }}>
                        💬 {t("追问")}：{a.followup_question}
                      </div>
                      <div className="px-3 py-2.5 text-[12.5px] leading-relaxed crashguard-md"
                        style={{ color: D.text1 }}>
                        {a.status === "success" ? (
                          <MarkdownText>{a.answer || "—"}</MarkdownText>
                        ) : a.status === "running" || a.status === "pending" ? (
                          <span style={{ color: D.text2 }}>⏳ {t("AI 正在思考...")}</span>
                        ) : a.status === "failed" ? (
                          <span style={{ color: D.danger }}>❌ {a.error || t("失败")}</span>
                        ) : (
                          <span style={{ color: D.text3 }}>—</span>
                        )}
                      </div>
                      {a.created_at && (
                        <div className="px-3 pb-2 text-[10px] font-mono" style={{ color: D.text3 }}>
                          {a.created_at.replace("T", " ").slice(0, 16)} · 🤖 {a.agent_model || a.agent_name || "agent"}
                        </div>
                      )}
                    </div>
                  ))}
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

const PIE_PALETTE = ["#B8922E", "#7C3AED", "#16A34A", "#DC2626", "#2563EB", "#D97706", "#9CA3AF"];

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
