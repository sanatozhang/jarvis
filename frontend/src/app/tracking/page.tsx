"use client";

import { useEffect, useState, useCallback } from "react";
import { fetchTracking, escalateIssue, type LocalIssueItem, type PaginatedResponse } from "@/lib/api";

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
      {ruleMatched && <span className="inline-flex rounded-full bg-emerald-100 px-1.5 py-0.5 text-[10px] font-bold text-emerald-700" title={`命中规则: ${ruleType}`}>100%</span>}
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

type FilterMode = "all" | "mine" | "custom";

export default function TrackingPage() {
  const [data, setData] = useState<PaginatedResponse<LocalIssueItem> | null>(null);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [toast, setToast] = useState("");
  const [detailItem, setDetailItem] = useState<LocalIssueItem | null>(null);

  // Filter state
  const [filterMode, setFilterMode] = useState<FilterMode>("all");
  const [customUser, setCustomUser] = useState("");
  const [customInput, setCustomInput] = useState("");
  const [showCustomInput, setShowCustomInput] = useState(false);

  const username = typeof window !== "undefined" ? localStorage.getItem("jarvis_username") || "" : "";

  const getFilterUser = (): string | undefined => {
    if (filterMode === "mine") return username;
    if (filterMode === "custom" && customUser) return customUser;
    return undefined;
  };

  const load = useCallback(async (p: number) => {
    setLoading(true);
    try { setData(await fetchTracking(p, 20, getFilterUser())); } catch {} finally { setLoading(false); }
  }, [filterMode, customUser, username]);

  useEffect(() => { load(page); }, [load, page]);

  const applyFilter = (mode: FilterMode) => {
    setFilterMode(mode);
    setPage(1);
    setShowCustomInput(false);
  };

  const applyCustom = () => {
    const v = customInput.trim();
    if (v) {
      setCustomUser(v);
      setFilterMode("custom");
      setPage(1);
    }
    setShowCustomInput(false);
  };

  const clearCustom = () => { setCustomUser(""); setFilterMode("all"); setPage(1); setShowCustomInput(false); };

  const copy = (text: string) => { navigator.clipboard.writeText(text); setToast("已复制到剪贴板"); };

  const handleEscalate = async (issueId: string) => {
    try {
      const res = await escalateIssue(issueId);
      setToast(res.status === "sent" ? "已通知值班工程师" : "暂无值班人员，请先配置值班表");
    } catch (e: any) { setToast(`通知失败: ${e.message}`); }
  };

  return (
    <div className="min-h-full">
      <header className="sticky top-0 z-10 border-b border-gray-200 bg-white/80 backdrop-blur-sm">
        <div className="flex items-center justify-between px-6 py-3">
          <h1 className="text-lg font-semibold">工单跟踪</h1>
          <div className="flex items-center gap-2">
            {/* Quick filters */}
            <div className="flex items-center gap-1 rounded-lg bg-gray-100 p-1">
              <button onClick={() => applyFilter("all")} className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${filterMode === "all" ? "bg-white text-gray-900 shadow-sm" : "text-gray-500 hover:text-gray-700"}`}>全部</button>
              <button onClick={() => applyFilter("mine")} className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${filterMode === "mine" ? "bg-white text-gray-900 shadow-sm" : "text-gray-500 hover:text-gray-700"}`}>我的</button>
              <button onClick={() => setShowCustomInput(true)} className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${filterMode === "custom" ? "bg-white text-gray-900 shadow-sm" : "text-gray-500 hover:text-gray-700"}`}>
                {filterMode === "custom" && customUser ? customUser : "指定人"}
              </button>
            </div>
            {/* Custom user input */}
            {showCustomInput && (
              <div className="flex items-center gap-1">
                <input autoFocus value={customInput} onChange={(e) => setCustomInput(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") applyCustom(); if (e.key === "Escape") setShowCustomInput(false); }}
                  placeholder="输入用户名" className="w-28 rounded-md border border-gray-300 px-2 py-1 text-xs outline-none focus:border-black" />
                <button onClick={applyCustom} className="rounded-md bg-black px-2 py-1 text-[11px] font-medium text-white">确定</button>
                {customUser && <button onClick={clearCustom} className="text-[11px] text-gray-400">清除</button>}
                <button onClick={() => setShowCustomInput(false)} className="text-[11px] text-gray-400">取消</button>
              </div>
            )}
          </div>
        </div>
      </header>

      <div className="px-6 py-5">
        {/* Stats bar */}
        {data && (
          <p className="mb-3 text-xs text-gray-400">
            共 {data.total} 个工单
            {filterMode === "mine" && ` (提交人: ${username})`}
            {filterMode === "custom" && customUser && ` (提交人: ${customUser})`}
          </p>
        )}

        <div className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
          <table className="min-w-full">
            <thead>
              <tr className="border-b border-gray-100 bg-gray-50/50">
                <th className="w-14 px-2 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">级别</th>
                <th className="px-4 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">问题描述</th>
                <th className="w-20 px-3 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">AI 状态</th>
                <th className="w-24 px-3 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">提交人</th>
                <th className="w-20 px-3 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">Zendesk</th>
                <th className="w-16 px-3 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider text-gray-400">飞书</th>
                <th className="w-40 px-4 py-2.5 text-right text-[11px] font-semibold uppercase tracking-wider text-gray-400">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-50">
              {loading && !data ? (
                <tr><td colSpan={7} className="px-4 py-16 text-center text-sm text-gray-300">加载中...</td></tr>
              ) : !data?.issues.length ? (
                <tr><td colSpan={7} className="px-4 py-16 text-center text-sm text-gray-300">
                  {filterMode === "mine" ? "你还没有提交过工单" : filterMode === "custom" ? `${customUser} 没有提交过工单` : "暂无工单"}
                </td></tr>
              ) : data.issues.map((item) => (
                <tr key={item.record_id} className="cursor-pointer hover:bg-gray-50/50" onClick={() => setDetailItem(item)}>
                  <td className="px-2 py-3 align-top"><PriorityBadge p={item.priority} /></td>
                  <td className="max-w-md px-4 py-3">
                    <p className="text-sm leading-snug text-gray-800" style={{ display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>{item.description}</p>
                    {item.root_cause_summary && (
                      <div className="mt-1.5 flex items-start gap-1.5">
                        <span className="mt-px flex-shrink-0 text-[10px] font-semibold text-amber-600">原因</span>
                        <p className="text-xs text-gray-500" style={{ display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>{item.root_cause_summary}</p>
                      </div>
                    )}
                  </td>
                  <td className="px-3 py-3 align-top"><StatusBadge status={item.local_status} ruleType={item.analysis?.rule_type} /></td>
                  <td className="px-3 py-3 align-top">
                    {item.created_by ? (
                      <button onClick={(e) => { e.stopPropagation(); setCustomUser(item.created_by!); setCustomInput(item.created_by!); setFilterMode("custom"); setPage(1); }}
                        className="text-xs text-blue-600 hover:underline">{item.created_by}</button>
                    ) : <span className="text-xs text-gray-300">—</span>}
                  </td>
                  <td className="px-3 py-3 align-top text-xs">{item.zendesk_id ? <a href={item.zendesk} target="_blank" onClick={(e) => e.stopPropagation()} className="font-medium text-blue-600 hover:underline">{item.zendesk_id}</a> : <span className="text-gray-300">—</span>}</td>
                  <td className="px-3 py-3 align-top text-xs">{item.feishu_link ? <a href={item.feishu_link} target="_blank" onClick={(e) => e.stopPropagation()} className="text-blue-500 hover:underline">链接</a> : "—"}</td>
                  <td className="px-4 py-3 align-top text-right" onClick={(e) => e.stopPropagation()}>
                    <div className="flex items-center justify-end gap-1.5">
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
                <div className="flex items-center gap-2 mb-3">
                  <PriorityBadge p={detailItem.priority} />
                  <StatusBadge status={detailItem.local_status} ruleType={detailItem.analysis?.rule_type} />
                  {detailItem.created_by && <span className="rounded bg-gray-100 px-2 py-0.5 text-[11px] text-gray-600">提交人: {detailItem.created_by}</span>}
                </div>
                <div className="grid grid-cols-2 gap-2 text-xs">
                  {[
                    { l: "设备 SN", v: detailItem.device_sn, m: true },
                    { l: "固件", v: detailItem.firmware },
                    { l: "APP", v: detailItem.app_version },
                    { l: "Zendesk", v: detailItem.zendesk_id },
                  ].map((f) => (
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
                  {detailItem.analysis.key_evidence && detailItem.analysis.key_evidence.length > 0 && (
                    <section>
                      <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-gray-400">关键证据</h3>
                      <div className="space-y-1">{detailItem.analysis.key_evidence.map((ev, i) => <div key={i} className="rounded bg-gray-50 px-3 py-1.5 font-mono text-[11px] text-gray-600">{ev}</div>)}</div>
                    </section>
                  )}
                  {detailItem.analysis.user_reply && (
                    <section>
                      <div className="mb-1.5 flex items-center justify-between">
                        <h3 className="text-xs font-semibold uppercase tracking-wider text-gray-400">建议回复</h3>
                        <button onClick={() => copy(detailItem.analysis!.user_reply)} className="rounded-md bg-green-600 px-3 py-1 text-[11px] font-medium text-white hover:bg-green-700">一键复制</button>
                      </div>
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
                <button onClick={() => handleEscalate(detailItem.record_id)} className="w-full rounded-lg border border-amber-300 bg-amber-50 py-2.5 text-sm font-medium text-amber-700 hover:bg-amber-100">转工程师处理</button>
                <p className="mt-1 text-center text-[11px] text-gray-400">通过飞书消息通知当前值班工程师</p>
              </section>
            </div>
          </div>
        </div>
      )}

      {toast && <Toast msg={toast} onClose={() => setToast("")} />}
    </div>
  );
}
