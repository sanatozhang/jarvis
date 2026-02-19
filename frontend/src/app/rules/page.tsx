"use client";

import { useT } from "@/lib/i18n";

import { useEffect, useState } from "react";
import { fetchRules, reloadRules, updateRule, type Rule } from "@/lib/api";

function Toast({ msg, onClose }: { msg: string; onClose: () => void }) {
  useEffect(() => { const t = setTimeout(onClose, 2500); return () => clearTimeout(t); }, [onClose]);
  return <div className="fixed bottom-6 right-6 z-50 rounded-lg bg-gray-900 px-4 py-2.5 text-sm font-medium text-white shadow-lg">{msg}</div>;
}

export default function RulesPage() {
  const t = useT();
  const [rules, setRules] = useState<Rule[]>([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<Rule | null>(null);
  const [editing, setEditing] = useState(false);
  const [editContent, setEditContent] = useState("");
  const [toast, setToast] = useState("");

  const load = async () => {
    setLoading(true);
    try {
      setRules(await fetchRules());
    } catch (e: any) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const handleReload = async () => {
    const r = await reloadRules();
    setToast(`${t("已重新加载")} ${r.reloaded} ${t("条规则")}`);
    await load();
  };

  const handleSave = async () => {
    if (!selected) return;
    try {
      await updateRule(selected.meta.id, { content: editContent });
      setToast("规则已保存");
      setEditing(false);
      await load();
    } catch (e: any) {
      setToast("保存失败: " + e.message);
    }
  };

  const handleToggle = async (rule: Rule) => {
    try {
      await updateRule(rule.meta.id, { enabled: !rule.meta.enabled });
      await load();
    } catch (e: any) {
      setToast("切换失败: " + e.message);
    }
  };

  return (
    <div className="min-h-full">
      <header className="sticky top-0 z-10 border-b border-gray-200 bg-white/80 backdrop-blur-sm">
        <div className="flex items-center justify-between px-6 py-3">
          <h1 className="text-lg font-semibold">{t("分析规则")}</h1>
          <div className="flex items-center gap-2">
            <button onClick={handleReload} className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm font-medium text-gray-600 hover:bg-gray-50">
              重新加载
            </button>
          </div>
        </div>
      </header>

      <div className="flex">
        {/* Rule list */}
        <div className="w-80 flex-shrink-0 border-r border-gray-100 bg-white">
          <div className="p-3">
            <p className="px-2 pb-2 text-xs font-semibold uppercase tracking-wider text-gray-400">
              {rules.length} {t("条规则")}
            </p>
            {loading ? (
              <p className="px-2 py-8 text-center text-sm text-gray-300">加载中...</p>
            ) : (
              <div className="space-y-0.5">
                {rules.map((rule) => (
                  <button
                    key={rule.meta.id}
                    onClick={() => { setSelected(rule); setEditing(false); setEditContent(rule.content); }}
                    className={`w-full rounded-lg px-3 py-2.5 text-left transition-colors ${
                      selected?.meta.id === rule.meta.id ? "bg-gray-100" : "hover:bg-gray-50"
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <span className="text-sm font-medium text-gray-800">{rule.meta.name || rule.meta.id}</span>
                      <span className={`h-2 w-2 rounded-full ${rule.meta.enabled ? "bg-green-400" : "bg-gray-300"}`} />
                    </div>
                    <div className="mt-1 flex flex-wrap gap-1">
                      {rule.meta.triggers.keywords.slice(0, 3).map((kw) => (
                        <span key={kw} className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-500">{kw}</span>
                      ))}
                      {rule.meta.triggers.keywords.length > 3 && (
                        <span className="text-[10px] text-gray-400">+{rule.meta.triggers.keywords.length - 3}</span>
                      )}
                    </div>
                    <p className="mt-1 text-[11px] text-gray-400">
                      优先级 {rule.meta.triggers.priority} · v{rule.meta.version}
                      {rule.meta.needs_code && " · 需代码"}
                    </p>
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* Rule detail */}
        <div className="flex-1 px-6 py-5">
          {!selected ? (
            <div className="flex h-64 items-center justify-center text-sm text-gray-300">
              选择一{t("条规则")}查看详情
            </div>
          ) : (
            <div>
              <div className="mb-4 flex items-start justify-between">
                <div>
                  <h2 className="text-xl font-bold">{selected.meta.name || selected.meta.id}</h2>
                  <p className="mt-1 text-xs text-gray-400">
                    ID: {selected.meta.id} · 版本 {selected.meta.version} · 优先级 {selected.meta.triggers.priority}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => handleToggle(selected)}
                    className={`rounded-lg px-3 py-1.5 text-xs font-medium transition-colors ${
                      selected.meta.enabled
                        ? "bg-green-50 text-green-700 hover:bg-green-100"
                        : "bg-gray-100 text-gray-500 hover:bg-gray-200"
                    }`}
                  >
                    {selected.meta.enabled ? t("已启用") : t("已禁用")}
                  </button>
                  {editing ? (
                    <>
                      <button onClick={handleSave} className="rounded-lg bg-black px-3 py-1.5 text-xs font-medium text-white hover:bg-gray-800">{t("保存")}</button>
                      <button onClick={() => setEditing(false)} className="rounded-lg border border-gray-200 px-3 py-1.5 text-xs font-medium text-gray-600 hover:bg-gray-50">{t("取消")}</button>
                    </>
                  ) : (
                    <button onClick={() => { setEditing(true); setEditContent(selected.content); }} className="rounded-lg border border-gray-200 px-3 py-1.5 text-xs font-medium text-gray-600 hover:bg-gray-50">
                      编辑
                    </button>
                  )}
                </div>
              </div>

              {/* Metadata cards */}
              <div className="mb-5 grid grid-cols-3 gap-3">
                <div className="rounded-lg border border-gray-100 bg-white p-3">
                  <p className="text-[11px] font-semibold uppercase tracking-wider text-gray-400">{t("触发关键词")}</p>
                  <div className="mt-1.5 flex flex-wrap gap-1">
                    {selected.meta.triggers.keywords.map((kw) => (
                      <span key={kw} className="rounded-md bg-blue-50 px-2 py-0.5 text-xs text-blue-600">{kw}</span>
                    ))}
                    {selected.meta.triggers.keywords.length === 0 && <span className="text-xs text-gray-300">无（兜底规则）</span>}
                  </div>
                </div>
                <div className="rounded-lg border border-gray-100 bg-white p-3">
                  <p className="text-[11px] font-semibold uppercase tracking-wider text-gray-400">{t("预提取模式")}</p>
                  <div className="mt-1.5 space-y-0.5">
                    {selected.meta.pre_extract.map((p) => (
                      <p key={p.name} className="font-mono text-[11px] text-gray-500">
                        {p.name}: <span className="text-gray-400">{p.pattern.slice(0, 30)}</span>
                      </p>
                    ))}
                    {selected.meta.pre_extract.length === 0 && <span className="text-xs text-gray-300">无</span>}
                  </div>
                </div>
                <div className="rounded-lg border border-gray-100 bg-white p-3">
                  <p className="text-[11px] font-semibold uppercase tracking-wider text-gray-400">{t("依赖 & 属性")}</p>
                  <div className="mt-1.5 space-y-0.5 text-xs text-gray-500">
                    <p>依赖: {selected.meta.depends_on.join(", ") || "无"}</p>
                    <p>需要代码: {selected.meta.needs_code ? "是" : "否"}</p>
                  </div>
                </div>
              </div>

              {/* Content */}
              {editing ? (
                <textarea
                  value={editContent}
                  onChange={(e) => setEditContent(e.target.value)}
                  className="h-[500px] w-full rounded-lg border border-gray-200 bg-white p-4 font-mono text-sm text-gray-700 outline-none focus:border-black focus:ring-1 focus:ring-black"
                />
              ) : (
                <div className="whitespace-pre-wrap rounded-lg border border-gray-100 bg-white p-5 font-mono text-sm leading-relaxed text-gray-600">
                  {selected.content}
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {toast && <Toast msg={toast} onClose={() => setToast("")} />}
    </div>
  );
}
