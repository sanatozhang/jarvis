"use client";

import { useEffect, useState, useCallback } from "react";
import { useT } from "@/lib/i18n";
import { fetchTracking, escalateIssue, type LocalIssueItem, type PaginatedResponse, type TrackingFilters } from "@/lib/api";

const CATEGORIES = [
  "硬件交互（蓝牙连接，固件升级，文件传输，音频播放，音频剪辑、音质不佳等）",
  "文件首页（首页所有功能，列表显示，移动文件夹，批量转写，重命名，合并音频，删除文件，导入音频，时钟问题导致文件名不一致）",
  "文件管理（转写，总结，文件编辑，分享导出，更多菜单，ASK Plaud，PCS）",
  "用户系统与管理（账号登录注册，Onboarding，个人资料，偏好设置，app push 通知）",
  "商业化（会员购买，会员转化）",
  "其他通用模块（Autoflow，模版社区，Plaud WEB、集成、功能许愿池、推荐朋友、隐私与安全、帮助与支持等其他功能）",
  "iZYREC 硬件问题",
];

// Short labels for display
const CATEGORY_SHORT: Record<string, string> = {};
CATEGORIES.forEach((c) => { CATEGORY_SHORT[c] = c.split("（")[0]; });

function StatusBadge({ status, ruleType }: { status: string; ruleType?: string }) {
  const m: Record<string, { bg: string; label: string }> = {
    analyzing: { bg: "bg-blue-50 text-blue-600 ring-blue-200", label: "分析中" },
    done: { bg: "bg-green-50 text-green-700 ring-green-200", label: "成功" },
    failed: { bg: "bg-red-50 text-red-600 ring-red-200", label: "失败" },
  };
  const s = m[status] || { bg: "bg-gray-50 text-gray-500 ring-gray-200", label: status };
  const ruleMatched = status === "done" && ruleType && ruleType !== "general";
  return (
    <span className="inline-flex items-center gap-1">
      <span className={`inline-flex rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ${s.bg}`}>{s.label}</span>
      {ruleMatched && <span className="inline-flex rounded-full bg-emerald-100 px-1.5 py-0.5 text-[10px] font-bold text-emerald-700">100%</span>}
    </span>
  );
}
function PriorityBadge({ p }: { p: string }) {
  return p === "H"
    ? <span className="inline-flex items-center rounded-full bg-red-50 px-2 py-0.5 text-[11px] font-semibold text-red-600 ring-1 ring-red-200">高</span>
    : <span className="inline-flex items-center rounded-full bg-gray-50 px-2 py-0.5 text-[11px] font-medium text-gray-500 ring-1 ring-gray-200">低</span>;
}
function Toast({ msg, onClose }: { msg: string; onClose: () => void }) {
  useEffect(() => { const t = setTimeout(onClose, 2500); return () => clearTimeout(t); }, [onClose]);
  return <div className="fixed bottom-6 right-6 z-50 rounded-lg bg-gray-900 px-4 py-2.5 text-sm font-medium text-white shadow-lg">{msg}</div>;
}
function Pagination({ page, totalPages, onChange }: { page: number; totalPages: number; onChange: (p: number) => void }) {
  if (totalPages <= 1) return null;
  return (
    <div className="mt-4 flex items-center justify-center gap-2">
      <button disabled={page <= 1} onClick={() => onChange(page - 1)} className="rounded-md border border-gray-200 px-3 py-1.5 text-xs font-medium text-gray-600 hover:bg-gray-50 disabled:opacity-30">上一页</button>
      <span className="text-xs tabular-nums text-gray-400">{page} / {totalPages}</span>
      <button disabled={page >= totalPages} onClick={() => onChange(page + 1)} className="rounded-md border border-gray-200 px-3 py-1.5 text-xs font-medium text-gray-600 hover:bg-gray-50 disabled:opacity-30">下一页</button>
    </div>
  );
}

export default function TrackingPage() {
  const t = useT();
  const [data, setData] = useState<PaginatedResponse<LocalIssueItem> | null>(null);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [toast, setToast] = useState("");
  const [detailItem, setDetailItem] = useState<LocalIssueItem | null>(null);

  // Filters
  const [filters, setFilters] = useState<TrackingFilters>({});
  const [showFilters, setShowFilters] = useState(false);
  const username = typeof window !== "undefined" ? localStorage.getItem("jarvis_username") || "" : "";

  const activeFilterCount = Object.values(filters).filter(Boolean).length;

  const load = useCallback(async (p: number) => {
    setLoading(true);
    try { setData(await fetchTracking(p, 20, filters)); } catch {} finally { setLoading(false); }
  }, [filters]);

  useEffect(() => { load(page); }, [load, page]);

  const updateFilter = (key: keyof TrackingFilters, val: string) => {
    setFilters((prev) => {
      const next = { ...prev, [key]: val || undefined };
      if (!val) delete next[key];
      return next;
    });
    setPage(1);
  };

  const clearFilters = () => { setFilters({}); setPage(1); };
  const setMyIssues = () => { setFilters({ created_by: username }); setPage(1); };

  const copy = (text: string) => { navigator.clipboard.writeText(text); setToast("已复制到剪贴板"); };
  const handleRetry = async (issueId: string) => {
    try {
      const res = await fetch("/api/tasks", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ issue_id: issueId }) });
      if (!res.ok) throw new Error(await res.text());
      setToast("已重新触发分析");
      setTimeout(() => load(page), 2000);
    } catch (e: any) { setToast(`重试失败: ${e.message}`); }
  };

  const handleEscalate = async (issueId: string) => {
    try {
      const res: any = await escalateIssue(issueId);
      setToast(res.message || "已通知");
    } catch (e: any) { setToast(`通知失败: ${e.message}`); }
  };

  return (
    <div className="min-h-full">
      <header className="sticky top-0 z-10 border-b border-gray-200 bg-white/80 backdrop-blur-sm">
        <div className="flex items-center justify-between px-6 py-3">
          <h1 className="text-lg font-semibold">工单跟踪</h1>
          <div className="flex items-center gap-2">
            <div className="flex items-center gap-1 rounded-lg bg-gray-100 p-1">
              <button onClick={clearFilters} className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${activeFilterCount === 0 ? "bg-white text-gray-900 shadow-sm" : "text-gray-500 hover:text-gray-700"}`}>全部</button>
              <button onClick={setMyIssues} className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${filters.created_by === username ? "bg-white text-gray-900 shadow-sm" : "text-gray-500 hover:text-gray-700"}`}>我的</button>
            </div>
            <button onClick={() => setShowFilters(!showFilters)} className={`rounded-lg border px-3 py-1.5 text-sm font-medium transition-colors ${showFilters || activeFilterCount > 0 ? "border-black bg-black text-white" : "border-gray-200 text-gray-600 hover:bg-gray-50"}`}>
              筛选{activeFilterCount > 0 && ` (${activeFilterCount})`}
            </button>
          </div>
        </div>

        {/* Filter bar */}
        {showFilters && (
          <div className="border-t border-gray-100 bg-gray-50/50 px-6 py-3">
            <div className="flex flex-wrap items-end gap-3">
              {/* Created by */}
              <div className="w-32">
                <label className="mb-1 block text-[10px] font-semibold uppercase tracking-wider text-gray-400">提交人</label>
                <input value={filters.created_by || ""} onChange={(e) => updateFilter("created_by", e.target.value)} placeholder="用户名"
                  className="w-full rounded-md border border-gray-200 px-2.5 py-1.5 text-xs outline-none focus:border-black" />
              </div>
              {/* Platform */}
              <div className="w-28">
                <label className="mb-1 block text-[10px] font-semibold uppercase tracking-wider text-gray-400">平台</label>
                <select value={filters.platform || ""} onChange={(e) => updateFilter("platform", e.target.value)}
                  className="w-full rounded-md border border-gray-200 px-2.5 py-1.5 text-xs outline-none focus:border-black">
                  <option value="">全部</option>
                  <option value="APP">APP</option>
                  <option value="Web">Web</option>
                  <option value="Desktop">Desktop</option>
                </select>
              </div>
              {/* Category */}
              <div className="w-44">
                <label className="mb-1 block text-[10px] font-semibold uppercase tracking-wider text-gray-400">问题分类</label>
                <select value={filters.category || ""} onChange={(e) => updateFilter("category", e.target.value)}
                  className="w-full rounded-md border border-gray-200 px-2.5 py-1.5 text-xs outline-none focus:border-black">
                  <option value="">全部分类</option>
                  {CATEGORIES.map((c) => <option key={c} value={c}>{CATEGORY_SHORT[c] || c}</option>)}
                </select>
              </div>
              {/* Status */}
              <div className="w-24">
                <label className="mb-1 block text-[10px] font-semibold uppercase tracking-wider text-gray-400">状态</label>
                <select value={filters.status || ""} onChange={(e) => updateFilter("status", e.target.value)}
                  className="w-full rounded-md border border-gray-200 px-2.5 py-1.5 text-xs outline-none focus:border-black">
                  <option value="">全部</option>
                  <option value="analyzing">分析中</option>
                  <option value="done">成功</option>
                  <option value="failed">失败</option>
                </select>
              </div>
              {/* Date from */}
              <div className="w-32">
                <label className="mb-1 block text-[10px] font-semibold uppercase tracking-wider text-gray-400">起始日期</label>
                <input type="date" value={filters.date_from || ""} onChange={(e) => updateFilter("date_from", e.target.value)}
                  className="w-full rounded-md border border-gray-200 px-2 py-1.5 text-xs outline-none focus:border-black" />
              </div>
              {/* Date to */}
              <div className="w-32">
                <label className="mb-1 block text-[10px] font-semibold uppercase tracking-wider text-gray-400">结束日期</label>
                <input type="date" value={filters.date_to || ""} onChange={(e) => updateFilter("date_to", e.target.value)}
                  className="w-full rounded-md border border-gray-200 px-2 py-1.5 text-xs outline-none focus:border-black" />
              </div>
              {/* Clear */}
              {activeFilterCount > 0 && (
                <button onClick={clearFilters} className="rounded-md px-2.5 py-1.5 text-xs text-red-500 hover:bg-red-50">清除筛选</button>
              )}
            </div>
          </div>
        )}
      </header>

      <div className="px-6 py-5">
        {data && (
          <p className="mb-3 text-xs text-gray-400">
            共 {data.total} 个工单
            {activeFilterCount > 0 && (
              <span className="ml-2">
                {filters.created_by && <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px]">提交人: {filters.created_by}</span>}
                {filters.platform && <span className="ml-1 rounded bg-blue-50 px-1.5 py-0.5 text-[10px] text-blue-600">{filters.platform}</span>}
                {filters.category && <span className="ml-1 rounded bg-amber-50 px-1.5 py-0.5 text-[10px] text-amber-600">{CATEGORY_SHORT[filters.category] || filters.category}</span>}
                {filters.status && <span className="ml-1 rounded bg-gray-100 px-1.5 py-0.5 text-[10px]">{filters.status}</span>}
              </span>
            )}
          </p>
        )}

        <div className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
          <table className="min-w-full">
            <thead>
              <tr className="border-b border-gray-100 bg-gray-50/50">
                <th className="w-14 px-2 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">级别</th>
                <th className="px-4 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">问题描述</th>
                <th className="w-20 px-3 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">状态</th>
                <th className="w-16 px-3 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">平台</th>
                <th className="w-24 px-3 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">提交人</th>
                <th className="w-20 px-3 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">Zendesk</th>
                <th className="w-36 px-4 py-2.5 text-right text-[11px] font-semibold uppercase tracking-wider text-gray-400">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-50">
              {loading && !data ? (
                <tr><td colSpan={7} className="px-4 py-16 text-center text-sm text-gray-300">加载中...</td></tr>
              ) : !data?.issues.length ? (
                <tr><td colSpan={7} className="px-4 py-16 text-center text-sm text-gray-300">暂无工单</td></tr>
              ) : data.issues.map((item) => (
                <tr key={item.record_id} className="cursor-pointer hover:bg-gray-50/50" onClick={() => setDetailItem(item)}>
                  <td className="px-2 py-3 align-top"><PriorityBadge p={item.priority} /></td>
                  <td className="max-w-md px-4 py-3">
                    <p className="text-sm leading-snug text-gray-800" style={{ display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>{item.description}</p>
                    {item.root_cause_summary && (
                      <div className="mt-1.5 flex items-start gap-1.5">
                        <span className="mt-px flex-shrink-0 text-[10px] font-semibold text-amber-600">原因</span>
                        <p className="text-xs text-gray-500" style={{ display: "-webkit-box", WebkitLineClamp: 1, WebkitBoxOrient: "vertical", overflow: "hidden" }}>{item.root_cause_summary}</p>
                      </div>
                    )}
                    {(item as any).category && (
                      <span className="mt-1 inline-block rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-500">{CATEGORY_SHORT[(item as any).category] || (item as any).category}</span>
                    )}
                  </td>
                  <td className="px-3 py-3 align-top"><StatusBadge status={item.local_status} ruleType={item.analysis?.rule_type} /></td>
                  <td className="px-3 py-3 align-top text-xs text-gray-500">{(item as any).platform || "—"}</td>
                  <td className="px-3 py-3 align-top">
                    {item.created_by ? (
                      <button onClick={(e) => { e.stopPropagation(); updateFilter("created_by", item.created_by!); }}
                        className="text-xs text-blue-600 hover:underline">{item.created_by}</button>
                    ) : <span className="text-xs text-gray-300">—</span>}
                  </td>
                  <td className="px-3 py-3 align-top text-xs">{item.zendesk_id ? <a href={item.zendesk} target="_blank" onClick={(e) => e.stopPropagation()} className="font-medium text-blue-600 hover:underline">{item.zendesk_id}</a> : <span className="text-gray-300">—</span>}</td>
                  <td className="px-4 py-3 align-top text-right" onClick={(e) => e.stopPropagation()}>
                    <div className="flex items-center justify-end gap-1.5">
                      {item.local_status === "failed" && <button onClick={() => handleRetry(item.record_id)} className="rounded-md bg-black px-2.5 py-1 text-[11px] font-medium text-white hover:bg-gray-800">重试</button>}
                      {item.analysis?.user_reply && <button onClick={() => copy(item.analysis!.user_reply)} className="rounded-md bg-green-600 px-2.5 py-1 text-[11px] font-medium text-white hover:bg-green-700">复制回复</button>}
                      <button onClick={() => handleEscalate(item.record_id)} className="rounded-md border border-amber-300 bg-amber-50 px-2.5 py-1 text-[11px] font-medium text-amber-700 hover:bg-amber-100">转工程师</button>
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
          <div className="flex-1 bg-black/20" onClick={() => setDetailItem(null)} />
          <div className="w-[520px] flex-shrink-0 overflow-y-auto border-l border-gray-200 bg-white shadow-2xl">
            <div className="sticky top-0 z-10 flex items-center justify-between border-b border-gray-100 bg-white px-5 py-3">
              <h2 className="text-sm font-semibold text-gray-800">工单详情</h2>
              <button onClick={() => setDetailItem(null)} className="rounded-lg p-1 text-gray-400 hover:bg-gray-100"><svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" /></svg></button>
            </div>
            <div className="p-5 space-y-5">
              <section>
                <div className="flex flex-wrap items-center gap-2 mb-3">
                  <PriorityBadge p={detailItem.priority} />
                  <StatusBadge status={detailItem.local_status} ruleType={detailItem.analysis?.rule_type} />
                  {(detailItem as any).platform && <span className="rounded bg-blue-50 px-2 py-0.5 text-[11px] text-blue-600">{(detailItem as any).platform}</span>}
                  {detailItem.created_by && <span className="rounded bg-gray-100 px-2 py-0.5 text-[11px] text-gray-600">{detailItem.created_by}</span>}
                  {(detailItem as any).created_at && <span className="text-[11px] text-gray-400">{(detailItem as any).created_at.slice(0, 10)}</span>}
                </div>
                {(detailItem as any).category && (
                  <p className="mb-2 text-xs text-gray-500">分类: <span className="font-medium text-gray-600">{CATEGORY_SHORT[(detailItem as any).category] || (detailItem as any).category}</span></p>
                )}
                <div className="grid grid-cols-2 gap-2 text-xs">
                  {[{ l: "设备 SN", v: detailItem.device_sn, m: true }, { l: "固件", v: detailItem.firmware }, { l: "APP", v: detailItem.app_version }, { l: "Zendesk", v: detailItem.zendesk_id }].map((f) => (
                    <div key={f.l} className="rounded-lg bg-gray-50 px-3 py-2"><span className="text-gray-400">{f.l}</span><p className={`mt-0.5 font-medium text-gray-700 ${f.m ? "font-mono" : ""}`}>{f.v || "—"}</p></div>
                  ))}
                </div>
              </section>
              <section>
                <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-gray-400">问题描述</h3>
                <div className="whitespace-pre-wrap rounded-lg bg-gray-50 p-3 text-sm leading-relaxed text-gray-700">{detailItem.description}</div>
              </section>
              {detailItem.analysis && (
                <>
                  <section>
                    <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-gray-400">问题原因</h3>
                    <div className="whitespace-pre-wrap rounded-lg bg-gray-50 p-3 text-sm text-gray-700">{detailItem.analysis.root_cause}</div>
                  </section>
                  {detailItem.analysis.user_reply && (
                    <section>
                      <div className="mb-1.5 flex items-center justify-between"><h3 className="text-xs font-semibold uppercase tracking-wider text-gray-400">建议回复</h3><button onClick={() => copy(detailItem.analysis!.user_reply)} className="rounded-md bg-green-600 px-3 py-1 text-[11px] font-medium text-white hover:bg-green-700">一键复制</button></div>
                      <div className="whitespace-pre-wrap rounded-lg border border-green-200 bg-green-50/50 p-3 text-sm text-gray-700">{detailItem.analysis.user_reply}</div>
                    </section>
                  )}
                </>
              )}
              {detailItem.task?.error && (
                <section>
                  <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-gray-400">失败原因</h3>
                  <div className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">{detailItem.task.error}</div>
                </section>
              )}
              <section className="border-t border-gray-100 pt-4">
                {detailItem.local_status === "failed" && (
                  <button onClick={() => { handleRetry(detailItem.record_id); setDetailItem(null); }} className="mb-2 w-full rounded-lg bg-black py-2.5 text-sm font-medium text-white hover:bg-gray-800">重新分析</button>
                )}
                <button onClick={() => handleEscalate(detailItem.record_id)} className="w-full rounded-lg border border-amber-300 bg-amber-50 py-2.5 text-sm font-medium text-amber-700 hover:bg-amber-100">转工程师处理</button>
              </section>
            </div>
          </div>
        </div>
      )}

      {toast && <Toast msg={toast} onClose={() => setToast("")} />}
    </div>
  );
}
