"use client";

import { useEffect, useState, useCallback } from "react";
import { useT, useLang } from "@/lib/i18n";
import { fetchTracking, markInaccurate, formatLocalTime, type LocalIssueItem, type PaginatedResponse, type TrackingFilters } from "@/lib/api";

const CATEGORIES_DATA = [
  { value: "硬件交互（蓝牙连接，固件升级，文件传输，音频播放，音频剪辑、音质不佳等）", cn: "硬件交互", en: "Hardware" },
  { value: "文件首页（首页所有功能，列表显示，移动文件夹，批量转写，重命名，合并音频，删除文件，导入音频，时钟问题导致文件名不一致）", cn: "文件首页", en: "File Home" },
  { value: "文件管理（转写，总结，文件编辑，分享导出，更多菜单，ASK Plaud，PCS）", cn: "文件管理", en: "File Mgmt" },
  { value: "用户系统与管理（账号登录注册，Onboarding，个人资料，偏好设置，app push 通知）", cn: "用户系统", en: "User System" },
  { value: "商业化（会员购买，会员转化）", cn: "商业化", en: "Monetization" },
  { value: "其他通用模块（Autoflow，模版社区，Plaud WEB、集成、功能许愿池、推荐朋友、隐私与安全、帮助与支持等其他功能）", cn: "其他", en: "Other" },
  { value: "iZYREC 硬件问题", cn: "iZYREC", en: "iZYREC" },
];
const CATEGORIES = CATEGORIES_DATA.map((c) => c.value);
const CATEGORY_SHORT: Record<string, string> = {};
const CATEGORY_SHORT_EN: Record<string, string> = {};
CATEGORIES_DATA.forEach((c) => { CATEGORY_SHORT[c.value] = c.cn; CATEGORY_SHORT_EN[c.value] = c.en; });

const S = {
  surface: "#F8F9FA", overlay: "#FFFFFF", hover: "#EEF0F2",
  border: "rgba(0,0,0,0.08)", borderSm: "rgba(0,0,0,0.04)",
  accent: "#B8922E", accentBg: "rgba(184,146,46,0.06)",
  text1: "#111827", text2: "#6B7280", text3: "#9CA3AF",
};

function StatusBadge({ status, ruleType }: { status: string; ruleType?: string }) {
  const t = useT();
  const cfg: Record<string, { bg: string; color: string; border: string; label: string }> = {
    analyzing: { bg: "rgba(96,165,250,0.12)", color: "#2563EB", border: "rgba(96,165,250,0.25)", label: t("分析中") },
    done:       { bg: "rgba(34,197,94,0.12)",  color: "#16A34A", border: "rgba(34,197,94,0.25)",  label: t("成功") },
    failed:     { bg: "rgba(239,68,68,0.12)",  color: "#DC2626", border: "rgba(239,68,68,0.25)",  label: t("失败") },
  };
  const s = cfg[status] || { bg: "rgba(0,0,0,0.04)", color: S.text3, border: S.border, label: status };
  const ruleMatched = status === "done" && ruleType && ruleType !== "general";
  return (
    <span className="inline-flex items-center gap-1">
      <span className="inline-flex rounded-full px-2 py-0.5 text-[10px] font-medium"
        style={{ background: s.bg, color: s.color, border: `1px solid ${s.border}` }}>
        {s.label}
      </span>
      {ruleMatched && (
        <span className="inline-flex rounded-full px-1.5 py-0.5 text-[9px] font-bold"
          style={{ background: "rgba(184,146,46,0.15)", color: S.accent, border: "1px solid rgba(184,146,46,0.3)" }}>
          100%
        </span>
      )}
    </span>
  );
}

function SourceBadge({ source }: { source?: string }) {
  const t = useT();
  const config: Record<string, { bg: string; color: string; border: string; label: string }> = {
    feishu: { bg: "rgba(96,165,250,0.12)",   color: "#2563EB", border: "rgba(96,165,250,0.25)",   label: t("飞书") },
    local:  { bg: "rgba(251,146,60,0.12)",   color: "#EA580C", border: "rgba(251,146,60,0.25)",   label: t("网站提交") },
    linear: { bg: "rgba(167,139,250,0.12)",  color: "#7C3AED", border: "rgba(167,139,250,0.25)",  label: "Linear" },
    api:    { bg: "rgba(52,211,153,0.12)",   color: "#059669", border: "rgba(52,211,153,0.25)",   label: "API" },
  };
  const c = config[source || ""] || config.feishu;
  return (
    <span className="inline-flex rounded-full px-2 py-0.5 text-[10px] font-medium"
      style={{ background: c.bg, color: c.color, border: `1px solid ${c.border}` }}>
      {c.label}
    </span>
  );
}

function PriorityBadge({ p }: { p: string }) {
  const t = useT();
  return p === "H" ? (
    <span className="inline-flex rounded-full px-2 py-0.5 text-[10px] font-semibold"
      style={{ background: "rgba(239,68,68,0.15)", color: "#DC2626", border: "1px solid rgba(239,68,68,0.25)" }}>
      {t("高")}
    </span>
  ) : (
    <span className="inline-flex rounded-full px-2 py-0.5 text-[10px] font-medium"
      style={{ background: "rgba(0,0,0,0.04)", color: S.text3, border: `1px solid ${S.border}` }}>
      {t("低")}
    </span>
  );
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

  const [data, setData] = useState<PaginatedResponse<LocalIssueItem> | null>(null);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [toast, setToast] = useState("");
  const [detailItem, setDetailItem] = useState<LocalIssueItem | null>(null);

  const openDetail = (item: LocalIssueItem) => {
    setDetailItem(item);
    const url = new URL(window.location.href);
    url.searchParams.set("detail", item.record_id);
    window.history.replaceState({}, "", url.toString());
  };
  const closeDetail = () => {
    setDetailItem(null);
    const url = new URL(window.location.href);
    url.searchParams.delete("detail");
    window.history.replaceState({}, "", url.toString());
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

  useEffect(() => { load(page); }, [load, page]);

  useEffect(() => {
    if (!data) return;
    const urlDetail = new URLSearchParams(window.location.search).get("detail");
    if (urlDetail && !detailItem) {
      const item = data.issues.find((i) => i.record_id === urlDetail);
      if (item) setDetailItem(item);
    }
  }, [data]);

  const syncFiltersToUrl = (f: TrackingFilters) => {
    const url = new URL(window.location.href);
    const filterKeys: (keyof TrackingFilters)[] = ["created_by", "platform", "category", "status", "source", "date_from", "date_to"];
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
  const handleMarkInaccurate = async (issueId: string) => {
    try { await markInaccurate(issueId); setToast(t("已标记为不准确")); setTimeout(() => load(page), 500); }
    catch (e: any) { setToast(`${t("失败")}: ${e.message}`); }
  };

  const thStyle = { color: S.text3, fontSize: "10px", fontWeight: 600, textTransform: "uppercase" as const, letterSpacing: "0.08em", padding: "10px 12px" };
  const tdBase = "px-3 py-3 align-top";

  return (
    <div className="min-h-full">
      {/* Header */}
      <header className="sticky top-0 z-10 backdrop-blur-md"
        style={{ background: "rgba(255,255,255,0.92)", borderBottom: `1px solid ${S.border}` }}>
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
                ? { background: S.accent, color: "#0A0B0E" }
                : { border: `1px solid ${S.border}`, color: S.text2 }}>
              {t("筛选")}{activeFilterCount > 0 && ` (${activeFilterCount})`}
            </button>
          </div>
        </div>

        {/* Filter bar */}
        {showFilters && (
          <div className="px-6 py-3" style={{ borderTop: `1px solid ${S.border}`, background: "rgba(248,249,250,0.95)" }}>
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
            {t("共")} {data.total} {t("个工单")}
            {activeFilterCount > 0 && (
              <span className="ml-2 space-x-1">
                {filters.created_by && <span className="rounded px-1.5 py-0.5 text-[10px]" style={{ background: S.overlay, color: S.text2 }}>{t("提交人")}: {filters.created_by}</span>}
                {filters.platform && <span className="rounded px-1.5 py-0.5 text-[10px]" style={{ background: "rgba(96,165,250,0.1)", color: "#2563EB" }}>{filters.platform}</span>}
                {filters.category && <span className="rounded px-1.5 py-0.5 text-[10px]" style={{ background: S.accentBg, color: S.accent }}>{catShort(filters.category)}</span>}
                {filters.status && <span className="rounded px-1.5 py-0.5 text-[10px]" style={{ background: S.overlay, color: S.text2 }}>{filters.status}</span>}
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
              ) : data.issues.map((item, idx) => (
                <tr key={item.record_id}
                  className="cursor-pointer transition-colors"
                  style={{ borderBottom: `1px solid ${S.borderSm}`, background: idx % 2 === 0 ? "transparent" : "rgba(0,0,0,0.01)" }}
                  onClick={() => openDetail(item)}
                  onMouseEnter={(e) => (e.currentTarget.style.background = S.hover + "60")}
                  onMouseLeave={(e) => (e.currentTarget.style.background = idx % 2 === 0 ? "transparent" : "rgba(0,0,0,0.01)")}>
                  <td className={tdBase} style={{ width: "56px" }}><PriorityBadge p={item.priority} /></td>
                  <td className="px-3 py-3 max-w-md">
                    <p className="text-sm leading-snug" style={{ color: S.text1, display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>
                      {item.description}
                    </p>
                    {item.root_cause_summary && (
                      <div className="mt-1.5 flex items-start gap-1.5">
                        <span className="mt-px flex-shrink-0 text-[10px] font-semibold" style={{ color: S.accent }}>{t("原因")}</span>
                        <p className="text-xs" style={{ color: S.text2, display: "-webkit-box", WebkitLineClamp: 1, WebkitBoxOrient: "vertical", overflow: "hidden" }}>{item.root_cause_summary}</p>
                      </div>
                    )}
                    {item.category && (
                      <span className="mt-1 inline-block rounded px-1.5 py-0.5 text-[10px]"
                        style={{ background: S.overlay, color: S.text3 }}>{catShort(item.category || "")}</span>
                    )}
                  </td>
                  <td className={tdBase} style={{ width: "96px" }}>
                    <StatusBadge status={item.local_status} ruleType={item.analysis?.rule_type} />
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
                          style={{ background: "#F8F9FA", color: S.accent, border: `1px solid rgba(184,146,46,0.3)` }}>
                          {t("重试")}
                        </button>
                      )}
                      {item.analysis?.user_reply && (
                        <button onClick={() => copy(item.analysis!.user_reply)}
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
              ))}
            </tbody>
          </table>
        </div>
        <Pagination page={page} totalPages={data?.total_pages ?? 1} onChange={(p) => setPage(p)} />
      </div>

      {/* Detail panel */}
      {detailItem && (
        <div className="fixed inset-0 z-50 flex">
          <div className="flex-1 backdrop-blur-sm" style={{ background: "rgba(0,0,0,0.65)" }} onClick={closeDetail} />
          <div className="w-[520px] flex-shrink-0 overflow-y-auto" style={{ background: "#FFFFFF", borderLeft: `1px solid ${S.border}` }}>
            <div className="sticky top-0 z-10 flex items-center justify-between px-5 py-3"
              style={{ background: "#FFFFFF", borderBottom: `1px solid ${S.border}` }}>
              <h2 className="text-sm font-semibold" style={{ color: S.text1 }}>{t("工单详情")}</h2>
              <button onClick={closeDetail} className="rounded-lg p-1.5" style={{ color: S.text3 }}>
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
                  {detailItem.platform && (
                    <span className="rounded-full px-2 py-0.5 text-[10px]"
                      style={{ background: "rgba(96,165,250,0.1)", color: "#2563EB" }}>{detailItem.platform}</span>
                  )}
                  <SourceBadge source={detailItem.source} />
                  {detailItem.created_by && (
                    <span className="rounded-full px-2 py-0.5 text-[10px]"
                      style={{ background: S.overlay, color: S.text2 }}>{detailItem.created_by}</span>
                  )}
                </div>
                {detailItem.category && (
                  <p className="mb-2 text-xs" style={{ color: S.text2 }}>
                    {t("分类")}: <span style={{ color: S.text1 }}>{catShort(detailItem.category || "")}</span>
                  </p>
                )}
                <div className="grid grid-cols-2 gap-2 text-xs">
                  {[{ l: t("设备 SN"), v: detailItem.device_sn, m: true }, { l: t("固件"), v: detailItem.firmware }, { l: "APP", v: detailItem.app_version }, { l: "Zendesk", v: detailItem.zendesk_id }].map((f) => (
                    <div key={f.l} className="rounded-lg px-3 py-2" style={{ background: S.overlay }}>
                      <span style={{ color: S.text3 }}>{f.l}</span>
                      <p className={`mt-0.5 font-medium ${f.m ? "font-mono" : ""}`} style={{ color: S.text1 }}>{f.v || "—"}</p>
                    </div>
                  ))}
                </div>
              </section>
              <section>
                <h3 className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>{t("问题描述")}</h3>
                <div className="whitespace-pre-wrap rounded-lg p-3 text-sm leading-relaxed" style={{ background: S.overlay, color: S.text2 }}>
                  {detailItem.description}
                </div>
              </section>
              {detailItem.analysis && (
                <>
                  <section>
                    <h3 className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>{t("问题原因")}</h3>
                    <div className="whitespace-pre-wrap rounded-lg p-3 text-sm" style={{ background: S.overlay, color: S.text2 }}>
                      {detailItem.analysis.root_cause}
                    </div>
                  </section>
                  {detailItem.analysis.user_reply && (
                    <section>
                      <div className="mb-1.5 flex items-center justify-between">
                        <h3 className="text-[10px] font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>{t("建议回复")}</h3>
                        <button onClick={() => copy(detailItem.analysis!.user_reply)}
                          className="rounded-lg px-3 py-1 text-[11px] font-medium"
                          style={{ background: "rgba(34,197,94,0.12)", color: "#16A34A", border: "1px solid rgba(34,197,94,0.25)" }}>
                          {t("一键复制")}
                        </button>
                      </div>
                      <div className="whitespace-pre-wrap rounded-lg p-3 text-sm"
                        style={{ background: S.overlay, color: S.text2, borderLeft: "2px solid rgba(34,197,94,0.4)" }}>
                        {detailItem.analysis.user_reply}
                      </div>
                    </section>
                  )}
                </>
              )}
              {detailItem.task?.error && (
                <section>
                  <h3 className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>{t("失败原因")}</h3>
                  <div className="rounded-lg p-3 text-sm" style={{ background: "rgba(239,68,68,0.08)", color: "#DC2626", border: "1px solid rgba(239,68,68,0.2)" }}>
                    {detailItem.task.error}
                  </div>
                </section>
              )}
              <section className="pt-4" style={{ borderTop: `1px solid ${S.border}` }}>
                {detailItem.local_status === "failed" && (
                  <button onClick={() => { handleRetry(detailItem.record_id); closeDetail(); }}
                    className="mb-2 w-full rounded-lg py-2.5 text-sm font-semibold"
                    style={{ background: S.accent, color: "#0A0B0E" }}>
                    {t("重新分析")}
                  </button>
                )}
                {detailItem.local_status === "done" && (
                  <button onClick={() => { handleMarkInaccurate(detailItem.record_id); closeDetail(); }}
                    className="w-full rounded-lg py-2.5 text-sm font-medium"
                    style={{ background: "rgba(239,68,68,0.10)", color: "#DC2626", border: "1px solid rgba(239,68,68,0.25)" }}>
                    {t("标记为不准确")}
                  </button>
                )}
              </section>
            </div>
          </div>
        </div>
      )}

      {toast && <Toast msg={toast} onClose={() => setToast("")} />}
    </div>
  );
}
