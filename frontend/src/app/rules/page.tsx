"use client";

import { useT } from "@/lib/i18n";
import { useEffect, useState, useCallback } from "react";
import { Toast } from "@/components/Toast";
import { fetchRules, reloadRules, updateRule, createRule, deleteRule, type Rule, type RuleMeta } from "@/lib/api";

const S = {
  surface: "#F8F9FA", overlay: "#FFFFFF", hover: "#EEF0F2",
  border: "rgba(0,0,0,0.08)", borderSm: "rgba(0,0,0,0.04)",
  accent: "#B8922E", accentBg: "rgba(184,146,46,0.06)",
  text1: "#111827", text2: "#6B7280", text3: "#9CA3AF",
  danger: "#DC2626", dangerBg: "rgba(220,38,38,0.06)",
};


/* ─── Modal Shell ─── */
function Modal({ title, onClose, children, wide }: { title: string; onClose: () => void; children: React.ReactNode; wide?: boolean }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" onClick={onClose}>
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" />
      <div className={`relative rounded-2xl shadow-2xl flex flex-col ${wide ? "w-[720px]" : "w-[560px]"}`}
        style={{ background: S.overlay, border: `1px solid ${S.border}`, maxHeight: "85vh" }}
        onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between px-6 py-4" style={{ borderBottom: `1px solid ${S.border}` }}>
          <h2 className="text-base font-semibold" style={{ color: S.text1 }}>{title}</h2>
          <button onClick={onClose} className="text-lg leading-none px-1" style={{ color: S.text3 }}>✕</button>
        </div>
        <div className="flex-1 overflow-y-auto px-6 py-4">
          {children}
        </div>
      </div>
    </div>
  );
}

/* ─── Keyword Tag Input ─── */
function TagInput({ tags, onChange, placeholder }: { tags: string[]; onChange: (t: string[]) => void; placeholder?: string }) {
  const [input, setInput] = useState("");
  const add = () => {
    const items = input.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
    if (items.length) { onChange([...tags, ...items.filter((i) => !tags.includes(i))]); setInput(""); }
  };
  return (
    <div className="rounded-lg p-2 flex flex-wrap gap-1.5 items-center min-h-[38px]"
      style={{ background: S.surface, border: `1px solid ${S.border}` }}>
      {tags.map((tag) => (
        <span key={tag} className="flex items-center gap-1 rounded-md px-2 py-0.5 text-xs"
          style={{ background: "rgba(96,165,250,0.1)", color: "#93C5FD", border: "1px solid rgba(96,165,250,0.2)" }}>
          {tag}
          <button onClick={() => onChange(tags.filter((t) => t !== tag))} className="opacity-60 hover:opacity-100">×</button>
        </span>
      ))}
      <input className="flex-1 min-w-[120px] bg-transparent text-sm outline-none" style={{ color: S.text1 }}
        value={input} onChange={(e) => setInput(e.target.value)} placeholder={tags.length === 0 ? placeholder : ""}
        onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); add(); } }}
        onBlur={add} />
    </div>
  );
}

/* ─── Pre-extract Editor ─── */
interface PreExtractItem { name: string; pattern: string; date_filter: boolean }

function PreExtractEditor({ items, onChange }: { items: PreExtractItem[]; onChange: (v: PreExtractItem[]) => void }) {
  const t = useT();
  const update = (i: number, patch: Partial<PreExtractItem>) => {
    const next = [...items]; next[i] = { ...next[i], ...patch }; onChange(next);
  };
  return (
    <div className="space-y-2">
      {items.map((item, i) => (
        <div key={i} className="flex gap-2 items-start">
          <input className="rounded-lg px-2 py-1.5 text-xs w-28 outline-none"
            style={{ background: S.surface, border: `1px solid ${S.border}`, color: S.text1 }}
            placeholder={t("预提取名称")} value={item.name} onChange={(e) => update(i, { name: e.target.value })} />
          <input className="rounded-lg px-2 py-1.5 text-xs flex-1 font-mono outline-none"
            style={{ background: S.surface, border: `1px solid ${S.border}`, color: S.text1 }}
            placeholder={t("正则表达式")} value={item.pattern} onChange={(e) => update(i, { pattern: e.target.value })} />
          <label className="flex items-center gap-1 text-[10px] whitespace-nowrap pt-1.5" style={{ color: S.text3 }}>
            <input type="checkbox" checked={item.date_filter} onChange={(e) => update(i, { date_filter: e.target.checked })} />
            {t("日期过滤")}
          </label>
          <button onClick={() => onChange(items.filter((_, j) => j !== i))}
            className="text-xs px-1.5 py-1 rounded" style={{ color: S.danger }}>×</button>
        </div>
      ))}
      <button onClick={() => onChange([...items, { name: "", pattern: "", date_filter: false }])}
        className="text-xs px-2 py-1 rounded-lg" style={{ color: S.accent, border: `1px dashed ${S.border}` }}>
        + {t("添加模式")}
      </button>
    </div>
  );
}

/* ─── Field Label ─── */
function Label({ children }: { children: React.ReactNode }) {
  return <label className="block text-xs font-medium mb-1.5" style={{ color: S.text2 }}>{children}</label>;
}

/* ─── Shared form fields for create + edit ─── */
interface RuleFormData {
  id: string; name: string; keywords: string[]; priority: number;
  pre_extract: PreExtractItem[]; depends_on: string; needs_code: boolean; content: string;
}

function RuleFormFields({ data, onChange, showId }: { data: RuleFormData; onChange: (d: RuleFormData) => void; showId?: boolean }) {
  const t = useT();
  const set = <K extends keyof RuleFormData>(k: K, v: RuleFormData[K]) => onChange({ ...data, [k]: v });
  return (
    <div className="space-y-4">
      {showId && (
        <div>
          <Label>{t("规则 ID")}</Label>
          <input className="w-full rounded-lg px-3 py-2 text-sm font-mono outline-none"
            style={{ background: S.surface, border: `1px solid ${S.border}`, color: S.text1 }}
            placeholder="bluetooth-v2" value={data.id} onChange={(e) => set("id", e.target.value)} />
        </div>
      )}
      <div>
        <Label>{t("规则名称")}</Label>
        <input className="w-full rounded-lg px-3 py-2 text-sm outline-none"
          style={{ background: S.surface, border: `1px solid ${S.border}`, color: S.text1 }}
          value={data.name} onChange={(e) => set("name", e.target.value)} />
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <Label>{t("触发关键词（逗号或回车分隔）")}</Label>
          <TagInput tags={data.keywords} onChange={(v) => set("keywords", v)}
            placeholder={t("触发关键词（逗号或回车分隔）")} />
        </div>
        <div>
          <Label>{t("优先级")}</Label>
          <input type="number" min={0} max={10} className="w-full rounded-lg px-3 py-2 text-sm outline-none"
            style={{ background: S.surface, border: `1px solid ${S.border}`, color: S.text1 }}
            value={data.priority} onChange={(e) => set("priority", Number(e.target.value))} />
        </div>
      </div>
      <div>
        <Label>{t("预提取模式")}</Label>
        <PreExtractEditor items={data.pre_extract} onChange={(v) => set("pre_extract", v)} />
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <Label>{t("依赖规则（逗号分隔）")}</Label>
          <input className="w-full rounded-lg px-3 py-2 text-sm outline-none"
            style={{ background: S.surface, border: `1px solid ${S.border}`, color: S.text1 }}
            value={data.depends_on} onChange={(e) => set("depends_on", e.target.value)} />
        </div>
        <div className="flex items-end pb-1">
          <label className="flex items-center gap-2 text-sm cursor-pointer" style={{ color: S.text2 }}>
            <input type="checkbox" checked={data.needs_code} onChange={(e) => set("needs_code", e.target.checked)} />
            {t("需要代码")}
          </label>
        </div>
      </div>
      <div>
        <Label>{t("规则内容（Markdown）")}</Label>
        <textarea className="w-full h-48 rounded-lg p-3 font-mono text-sm outline-none resize-none"
          style={{ background: S.surface, border: `1px solid ${S.border}`, color: S.text1, lineHeight: "1.6" }}
          value={data.content} onChange={(e) => set("content", e.target.value)} />
      </div>
    </div>
  );
}

/* ─── Create Rule Modal ─── */
function CreateRuleModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const t = useT();
  const [form, setForm] = useState<RuleFormData>({
    id: "", name: "", keywords: [], priority: 5,
    pre_extract: [], depends_on: "", needs_code: false, content: "",
  });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const handleSubmit = async () => {
    if (!form.id.trim() || !form.name.trim() || !form.content.trim()) return;
    setSaving(true); setError("");
    try {
      await createRule({
        id: form.id.trim(), name: form.name.trim(), content: form.content,
        triggers: { keywords: form.keywords, priority: form.priority },
        pre_extract: form.pre_extract.filter((p) => p.name && p.pattern),
        depends_on: form.depends_on.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
        needs_code: form.needs_code,
      });
      onCreated();
    } catch (e: any) { setError(e.message); } finally { setSaving(false); }
  };

  return (
    <Modal title={t("新建规则")} onClose={onClose}>
      <RuleFormFields data={form} onChange={setForm} showId />
      {error && <p className="mt-3 text-xs" style={{ color: S.danger }}>{error}</p>}
      <div className="flex justify-end gap-2 mt-5">
        <button onClick={onClose} className="rounded-lg px-4 py-1.5 text-sm"
          style={{ border: `1px solid ${S.border}`, color: S.text2 }}>{t("取消")}</button>
        <button onClick={handleSubmit} disabled={saving || !form.id.trim() || !form.name.trim()}
          className="rounded-lg px-4 py-1.5 text-sm font-semibold disabled:opacity-40"
          style={{ background: S.accent, color: "#0A0B0E" }}>
          {saving ? t("加载中...") : t("创建")}
        </button>
      </div>
    </Modal>
  );
}

/* ─── Help Doc Modal ─── */
function HelpModal({ onClose }: { onClose: () => void }) {
  const t = useT();
  const sections = [
    {
      title: t("规则系统概述"),
      body: `规则引擎在分析工单时自动匹配：
• 用户提交工单 → 引擎扫描问题描述
• 按关键词匹配命中规则（可多个命中，按优先级排序）
• 命中规则的预提取模式在日志中 grep 关键信息
• 匹配到的规则和提取结果一起传给 AI Agent 分析`,
    },
    {
      title: t("字段说明"),
      body: `ID — 唯一英文标识（如 bluetooth-disconnect），创建后不可修改
名称 — 规则的可读名称
触发关键词 — 问题描述中出现这些词时命中此规则（空 = 兜底规则）
优先级 — 0-10，数值越大越优先（默认 5）
预提取模式 — 在日志中 grep 的正则表达式，提取结果附加到 prompt
  • name: 提取项名称
  • pattern: grep 正则
  • date_filter: 是否只取最近日期的匹配
依赖规则 — 此规则依赖的其他规则 ID，被依赖规则会一并匹配
需要代码 — 勾选后 Agent 会附带代码仓库上下文
规则内容 — Markdown 格式，描述问题分析方法和排查步骤`,
    },
    {
      title: t("规则内容编写指南"),
      body: `规则内容使用 Markdown 编写，建议结构：

## 问题描述
简要说明此类问题的表现

## 排查步骤
1. 第一步：检查 xxx 日志
2. 第二步：确认 xxx 状态
3. ...

## 常见原因
- 原因 A：描述 + 对应日志特征
- 原因 B：描述 + 对应日志特征

## 回复模板
给用户的标准回复文本

Tips:
• 尽量具体，列出要在日志中查找的关键字
• 预提取模式配合内容中的排查步骤使用
• 回复模板帮助 AI 生成更准确的用户回复`,
    },
    {
      title: t("完整示例"),
      body: `ID: bluetooth-disconnect
名称: 蓝牙断连分析
关键词: 蓝牙, bluetooth, 断连, 断开, disconnect
优先级: 7
预提取模式:
  • bt_status / "BT state changed|bluetooth.*disconnect" / 日期过滤: 是
  • bt_error / "bluetooth.*error|hci.*fail" / 日期过滤: 否
依赖: device-basic
需要代码: 否

---

规则内容:

## 蓝牙断连问题分析

### 排查步骤
1. 查看 bt_status 提取结果，确认断连时序
2. 查看 bt_error 提取结果，确认是否有底层错误
3. 检查固件版本是否在已知问题列表中

### 常见原因
- 距离过远导致信号丢失：日志中看到 rssi 值持续下降
- 固件 BT 栈崩溃：日志中出现 hci timeout 或 controller error
- 手机端主动断开：日志中出现 remote disconnect reason 0x13`,
    },
  ];

  return (
    <Modal title={t("规则说明")} onClose={onClose} wide>
      <div className="space-y-5">
        {sections.map((sec) => (
          <div key={sec.title}>
            <h3 className="text-sm font-semibold mb-2" style={{ color: S.accent }}>{sec.title}</h3>
            <pre className="whitespace-pre-wrap text-xs leading-relaxed rounded-lg p-3"
              style={{ background: S.surface, color: S.text2, border: `1px solid ${S.borderSm}` }}>
              {sec.body}
            </pre>
          </div>
        ))}
      </div>
    </Modal>
  );
}

/* ─── Delete Confirm Modal ─── */
function DeleteConfirm({ ruleName, onConfirm, onClose }: { ruleName: string; onConfirm: () => void; onClose: () => void }) {
  const t = useT();
  return (
    <Modal title={t("删除")} onClose={onClose}>
      <p className="text-sm mb-1" style={{ color: S.text1 }}>
        {t("确定要删除规则吗？")}
      </p>
      <p className="text-sm mb-1" style={{ color: S.accent }}>{ruleName}</p>
      <p className="text-xs" style={{ color: S.text3 }}>{t("此操作不可撤销。")}</p>
      <div className="flex justify-end gap-2 mt-5">
        <button onClick={onClose} className="rounded-lg px-4 py-1.5 text-sm"
          style={{ border: `1px solid ${S.border}`, color: S.text2 }}>{t("取消")}</button>
        <button onClick={onConfirm} className="rounded-lg px-4 py-1.5 text-sm font-semibold"
          style={{ background: S.danger, color: "#fff" }}>{t("删除")}</button>
      </div>
    </Modal>
  );
}

/* ─── Main Page ─── */
export default function RulesPage() {
  const t = useT();
  const [rules, setRules] = useState<Rule[]>([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<Rule | null>(null);
  const [editing, setEditing] = useState(false);
  const [editForm, setEditForm] = useState<RuleFormData>({ id: "", name: "", keywords: [], priority: 5, pre_extract: [], depends_on: "", needs_code: false, content: "" });
  const [toast, setToast] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [showHelp, setShowHelp] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Rule | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try { setRules(await fetchRules()); } catch (e: any) { console.error(e); } finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  // Sync selected with latest rules data
  useEffect(() => {
    if (selected) {
      const updated = rules.find((r) => r.meta.id === selected.meta.id);
      if (updated) setSelected(updated);
      else setSelected(null);
    }
  }, [rules]); // eslint-disable-line react-hooks/exhaustive-deps

  const ruleToForm = (rule: Rule): RuleFormData => ({
    id: rule.meta.id, name: rule.meta.name, keywords: [...rule.meta.triggers.keywords],
    priority: rule.meta.triggers.priority, pre_extract: rule.meta.pre_extract.map((p) => ({ ...p })),
    depends_on: rule.meta.depends_on.join(", "), needs_code: rule.meta.needs_code, content: rule.content,
  });

  const handleReload = async () => {
    const r = await reloadRules();
    setToast(`${t("已重新加载")} ${r.reloaded} ${t("条规则")}`);
    await load();
  };

  const startEdit = (rule: Rule) => {
    setEditing(true);
    setEditForm(ruleToForm(rule));
  };

  const handleSave = async () => {
    if (!selected) return;
    try {
      await updateRule(selected.meta.id, {
        name: editForm.name, content: editForm.content,
        triggers: { keywords: editForm.keywords, priority: editForm.priority },
        pre_extract: editForm.pre_extract.filter((p) => p.name && p.pattern),
        depends_on: editForm.depends_on.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
        needs_code: editForm.needs_code,
      });
      setToast(t("规则已保存")); setEditing(false); await load();
    } catch (e: any) { setToast(t("保存失败") + ": " + e.message); }
  };

  const handleToggle = async (rule: Rule) => {
    try { await updateRule(rule.meta.id, { enabled: !rule.meta.enabled }); await load(); }
    catch (e: any) { setToast(t("切换失败") + ": " + e.message); }
  };

  const handleDelete = async () => {
    if (!deleteTarget) return;
    try {
      await deleteRule(deleteTarget.meta.id);
      setToast(t("规则已删除"));
      if (selected?.meta.id === deleteTarget.meta.id) { setSelected(null); setEditing(false); }
      setDeleteTarget(null); await load();
    } catch (e: any) { setToast(t("删除规则失败") + ": " + e.message); setDeleteTarget(null); }
  };

  const handleCreated = async () => {
    setToast(t("规则已创建")); setShowCreate(false); await load();
  };

  return (
    <div className="min-h-full flex flex-col">
      {/* Header */}
      <header className="sticky top-0 z-10 flex-shrink-0 backdrop-blur-md"
        style={{ background: "rgba(255,255,255,0.92)", borderBottom: `1px solid ${S.border}` }}>
        <div className="flex items-center justify-between px-6 py-3">
          <h1 className="text-base font-semibold" style={{ color: S.text1 }}>{t("分析规则")}</h1>
          <div className="flex items-center gap-2">
            <button onClick={() => setShowHelp(true)}
              className="rounded-lg px-3 py-1.5 text-sm font-medium transition-colors"
              style={{ border: `1px solid ${S.border}`, color: S.text2 }}>
              ? {t("规则说明")}
            </button>
            <button onClick={handleReload}
              className="rounded-lg px-3 py-1.5 text-sm font-medium transition-colors"
              style={{ border: `1px solid ${S.border}`, color: S.text2 }}>
              ↺ {t("重新加载")}
            </button>
            <button onClick={() => setShowCreate(true)}
              className="rounded-lg px-3 py-1.5 text-sm font-semibold transition-colors"
              style={{ background: S.accent, color: "#0A0B0E" }}>
              + {t("新建规则")}
            </button>
          </div>
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
                    onClick={() => { setSelected(rule); setEditing(false); }}
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
                        style={{ background: rule.meta.enabled ? "#16A34A" : S.hover,
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
          ) : editing ? (
            /* ── Edit Mode ── */
            <div>
              <div className="mb-5 flex items-center justify-between">
                <h2 className="text-xl font-bold" style={{ color: S.text1 }}>{t("编辑")} — {selected.meta.id}</h2>
                <div className="flex items-center gap-2">
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
                </div>
              </div>
              <RuleFormFields data={editForm} onChange={setEditForm} />
            </div>
          ) : (
            /* ── View Mode ── */
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
                  <button onClick={() => startEdit(selected)}
                    className="rounded-lg px-3 py-1.5 text-xs font-medium transition-colors"
                    style={{ border: `1px solid ${S.border}`, color: S.text2 }}>
                    {t("编辑")}
                  </button>
                  <button onClick={() => setDeleteTarget(selected)}
                    className="rounded-lg px-3 py-1.5 text-xs font-medium transition-colors"
                    style={{ border: `1px solid rgba(239,68,68,0.3)`, color: S.danger }}>
                    {t("删除")}
                  </button>
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
                        <span style={{ color: S.accent }}>{p.name}</span>: {p.pattern.length > 28 ? p.pattern.slice(0, 28) + "…" : p.pattern}
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

              {/* Content */}
              <div className="whitespace-pre-wrap rounded-xl p-5 font-mono text-sm leading-relaxed"
                style={{ background: S.surface, border: `1px solid ${S.border}`, color: S.text2 }}>
                {selected.content}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Modals */}
      {showCreate && <CreateRuleModal onClose={() => setShowCreate(false)} onCreated={handleCreated} />}
      {showHelp && <HelpModal onClose={() => setShowHelp(false)} />}
      {deleteTarget && <DeleteConfirm ruleName={deleteTarget.meta.name || deleteTarget.meta.id} onConfirm={handleDelete} onClose={() => setDeleteTarget(null)} />}
      {toast && <Toast msg={toast} onClose={() => setToast("")} />}
    </div>
  );
}
