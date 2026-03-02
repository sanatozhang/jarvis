"use client";

import { useT } from "@/lib/i18n";
import { useEffect, useState } from "react";
import { fetchRules, reloadRules, updateRule, type Rule } from "@/lib/api";

const S = {
  surface: "#111318", overlay: "#1A1D24", hover: "#22262F",
  border: "rgba(255,255,255,0.08)", borderSm: "rgba(255,255,255,0.05)",
  accent: "#D4A843", accentBg: "rgba(212,168,67,0.10)",
  text1: "#EBEBEF", text2: "#9898A8", text3: "#4A4A57",
};

function Toast({ msg, onClose }: { msg: string; onClose: () => void }) {
  useEffect(() => { const id = setTimeout(onClose, 2500); return () => clearTimeout(id); }, [onClose]);
  return (
    <div className="fixed bottom-6 right-6 z-50 rounded-xl px-4 py-2.5 text-sm font-medium shadow-2xl"
      style={{ background: S.surface, color: S.text1, border: `1px solid ${S.border}` }}>
      {msg}
    </div>
  );
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
    try { setRules(await fetchRules()); } catch (e: any) { console.error(e); } finally { setLoading(false); }
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
      setToast(t("规则已保存")); setEditing(false); await load();
    } catch (e: any) { setToast(t("保存失败") + ": " + e.message); }
  };

  const handleToggle = async (rule: Rule) => {
    try { await updateRule(rule.meta.id, { enabled: !rule.meta.enabled }); await load(); }
    catch (e: any) { setToast(t("切换失败") + ": " + e.message); }
  };

  return (
    <div className="min-h-full flex flex-col">
      {/* Header */}
      <header className="sticky top-0 z-10 flex-shrink-0 backdrop-blur-md"
        style={{ background: "rgba(10,11,14,0.92)", borderBottom: `1px solid ${S.border}` }}>
        <div className="flex items-center justify-between px-6 py-3">
          <h1 className="text-base font-semibold" style={{ color: S.text1 }}>{t("分析规则")}</h1>
          <button onClick={handleReload}
            className="rounded-lg px-3 py-1.5 text-sm font-medium transition-colors"
            style={{ border: `1px solid ${S.border}`, color: S.text2 }}>
            ↺ {t("重新加载")}
          </button>
        </div>
      </header>

      <div className="flex flex-1 min-h-0">
        {/* Rule list sidebar */}
        <div className="w-72 flex-shrink-0 overflow-y-auto" style={{ borderRight: `1px solid ${S.border}` }}>
          <div className="p-3">
            <p className="px-2 pb-2 text-[10px] font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>
              {rules.length} {t("条规则")}
            </p>
            {loading ? (
              <p className="px-2 py-8 text-center text-sm" style={{ color: S.text3 }}>{t("加载中...")}</p>
            ) : (
              <div className="space-y-px">
                {rules.map((rule) => (
                  <button key={rule.meta.id}
                    onClick={() => { setSelected(rule); setEditing(false); setEditContent(rule.content); }}
                    className="w-full rounded-lg px-3 py-2.5 text-left transition-all"
                    style={selected?.meta.id === rule.meta.id
                      ? { background: S.accentBg, borderLeft: `2px solid ${S.accent}` }
                      : { borderLeft: "2px solid transparent" }}
                    onMouseEnter={(e) => { if (selected?.meta.id !== rule.meta.id) (e.currentTarget as HTMLElement).style.background = S.hover + "40"; }}
                    onMouseLeave={(e) => { if (selected?.meta.id !== rule.meta.id) (e.currentTarget as HTMLElement).style.background = "transparent"; }}>
                    <div className="flex items-center justify-between">
                      <span className="text-sm font-medium" style={{ color: selected?.meta.id === rule.meta.id ? S.text1 : S.text2 }}>
                        {rule.meta.name || rule.meta.id}
                      </span>
                      <span className="h-2 w-2 rounded-full"
                        style={{ background: rule.meta.enabled ? "#22C55E" : S.hover,
                          boxShadow: rule.meta.enabled ? "0 0 4px rgba(34,197,94,0.4)" : "none" }} />
                    </div>
                    <div className="mt-1 flex flex-wrap gap-1">
                      {rule.meta.triggers.keywords.slice(0, 3).map((kw) => (
                        <span key={kw} className="rounded px-1.5 py-0.5 text-[10px]"
                          style={{ background: S.overlay, color: S.text3 }}>{kw}</span>
                      ))}
                      {rule.meta.triggers.keywords.length > 3 && (
                        <span className="text-[10px]" style={{ color: S.text3 }}>+{rule.meta.triggers.keywords.length - 3}</span>
                      )}
                    </div>
                    <p className="mt-1 text-[10px] font-mono" style={{ color: S.text3 }}>
                      P{rule.meta.triggers.priority} · v{rule.meta.version}
                      {rule.meta.needs_code && " · code"}
                    </p>
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* Rule detail */}
        <div className="flex-1 overflow-y-auto px-6 py-5">
          {!selected ? (
            <div className="flex h-48 items-center justify-center">
              <p className="text-sm" style={{ color: S.text3 }}>{t("选择一条规则查看详情")}</p>
            </div>
          ) : (
            <div>
              <div className="mb-5 flex items-start justify-between">
                <div>
                  <h2 className="text-xl font-bold" style={{ color: S.text1 }}>{selected.meta.name || selected.meta.id}</h2>
                  <p className="mt-1 text-xs font-mono" style={{ color: S.text3 }}>
                    ID: {selected.meta.id} · {t("版本")} {selected.meta.version} · {t("优先级")} {selected.meta.triggers.priority}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <button onClick={() => handleToggle(selected)}
                    className="rounded-lg px-3 py-1.5 text-xs font-medium transition-colors"
                    style={selected.meta.enabled
                      ? { background: "rgba(34,197,94,0.12)", color: "#4ADE80", border: "1px solid rgba(34,197,94,0.25)" }
                      : { background: S.overlay, color: S.text3, border: `1px solid ${S.border}` }}>
                    {selected.meta.enabled ? t("已启用") : t("已禁用")}
                  </button>
                  {editing ? (
                    <>
                      <button onClick={handleSave}
                        className="rounded-lg px-3 py-1.5 text-xs font-semibold"
                        style={{ background: S.accent, color: "#0A0B0E" }}>
                        {t("保存")}
                      </button>
                      <button onClick={() => setEditing(false)}
                        className="rounded-lg px-3 py-1.5 text-xs font-medium"
                        style={{ border: `1px solid ${S.border}`, color: S.text2 }}>
                        {t("取消")}
                      </button>
                    </>
                  ) : (
                    <button onClick={() => { setEditing(true); setEditContent(selected.content); }}
                      className="rounded-lg px-3 py-1.5 text-xs font-medium transition-colors"
                      style={{ border: `1px solid ${S.border}`, color: S.text2 }}>
                      {t("编辑")}
                    </button>
                  )}
                </div>
              </div>

              {/* Meta cards */}
              <div className="mb-5 grid grid-cols-3 gap-3">
                <div className="rounded-xl p-3" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
                  <p className="text-[10px] font-semibold uppercase tracking-wider mb-2" style={{ color: S.text3 }}>{t("触发关键词")}</p>
                  <div className="flex flex-wrap gap-1">
                    {selected.meta.triggers.keywords.map((kw) => (
                      <span key={kw} className="rounded-md px-2 py-0.5 text-xs"
                        style={{ background: "rgba(96,165,250,0.1)", color: "#93C5FD", border: "1px solid rgba(96,165,250,0.2)" }}>
                        {kw}
                      </span>
                    ))}
                    {selected.meta.triggers.keywords.length === 0 && (
                      <span className="text-xs" style={{ color: S.text3 }}>{t("无（兜底规则）")}</span>
                    )}
                  </div>
                </div>
                <div className="rounded-xl p-3" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
                  <p className="text-[10px] font-semibold uppercase tracking-wider mb-2" style={{ color: S.text3 }}>{t("预提取模式")}</p>
                  <div className="space-y-1">
                    {selected.meta.pre_extract.map((p) => (
                      <p key={p.name} className="font-mono text-[10px]" style={{ color: S.text3 }}>
                        <span style={{ color: S.accent }}>{p.name}</span>: {p.pattern.slice(0, 28)}…
                      </p>
                    ))}
                    {selected.meta.pre_extract.length === 0 && (
                      <span className="text-xs" style={{ color: S.text3 }}>{t("无")}</span>
                    )}
                  </div>
                </div>
                <div className="rounded-xl p-3" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
                  <p className="text-[10px] font-semibold uppercase tracking-wider mb-2" style={{ color: S.text3 }}>{t("依赖 & 属性")}</p>
                  <div className="space-y-0.5 text-xs" style={{ color: S.text2 }}>
                    <p>{t("依赖")}: <span style={{ color: S.text1 }}>{selected.meta.depends_on.join(", ") || t("无")}</span></p>
                    <p>{t("需要代码")}: <span style={{ color: selected.meta.needs_code ? S.accent : S.text3 }}>{selected.meta.needs_code ? t("是") : t("否")}</span></p>
                  </div>
                </div>
              </div>

              {/* Content / editor */}
              {editing ? (
                <textarea value={editContent} onChange={(e) => setEditContent(e.target.value)}
                  className="h-[500px] w-full rounded-xl p-4 font-mono text-sm outline-none resize-none"
                  style={{ background: S.surface, border: `1px solid ${S.border}`, color: S.text1, lineHeight: "1.6" }} />
              ) : (
                <div className="whitespace-pre-wrap rounded-xl p-5 font-mono text-sm leading-relaxed"
                  style={{ background: S.surface, border: `1px solid ${S.border}`, color: S.text2 }}>
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
