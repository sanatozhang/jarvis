"use client";

import { useT } from "@/lib/i18n";
import { useEffect, useState } from "react";
import { getOncallSchedule, getOncallCurrent, updateOncallSchedule, type OncallGroup } from "@/lib/api";

const S = {
  surface: "#F8F9FA", overlay: "#FFFFFF", hover: "#EEF0F2",
  border: "rgba(0,0,0,0.08)", accent: "#B8922E", accentBg: "rgba(184,146,46,0.06)",
  text1: "#111827", text2: "#6B7280", text3: "#9CA3AF",
};

function Toast({ msg, type, onClose }: { msg: string; type: "success" | "error"; onClose: () => void }) {
  useEffect(() => { const id = setTimeout(onClose, 3000); return () => clearTimeout(id); }, [onClose]);
  return (
    <div className="fixed bottom-6 right-6 z-50 rounded-xl px-4 py-2.5 text-sm font-medium shadow-2xl"
      style={{
        background: type === "success" ? "rgba(34,197,94,0.15)" : "rgba(239,68,68,0.15)",
        color: type === "success" ? "#16A34A" : "#DC2626",
        border: `1px solid ${type === "success" ? "rgba(34,197,94,0.3)" : "rgba(239,68,68,0.3)"}`,
      }}>
      {msg}
    </div>
  );
}

const inputStyle = { background: S.overlay, border: `1px solid ${S.border}`, color: S.text1, outline: "none" };

export default function OncallPage() {
  const t = useT();
  const [groups, setGroups] = useState<string[][]>([]);
  const [startDate, setStartDate] = useState("");
  const [currentMembers, setCurrentMembers] = useState<string[]>([]);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState<{ msg: string; type: "success" | "error" } | null>(null);

  const username = typeof window !== "undefined" ? localStorage.getItem("appllo_username") || "" : "";
  const isAdmin = username === "sanato";

  const load = async () => {
    try {
      const [sched, curr] = await Promise.all([getOncallSchedule(), getOncallCurrent()]);
      setGroups(sched.groups.map((g) => g.members));
      setStartDate(sched.start_date || new Date().toISOString().slice(0, 10));
      setCurrentMembers(curr.members);
    } catch (e: any) { setToast({ msg: e.message, type: "error" }); }
  };

  useEffect(() => { load(); }, []);

  const addGroup = () => setGroups((p) => [...p, [""]]);
  const removeGroup = (idx: number) => setGroups((p) => p.filter((_, i) => i !== idx));
  const updateMember = (gi: number, mi: number, val: string) => {
    setGroups((p) => p.map((g, i) => i === gi ? g.map((m, j) => j === mi ? val : m) : g));
  };
  const addMember = (gi: number) => setGroups((p) => p.map((g, i) => i === gi ? [...g, ""] : g));
  const removeMember = (gi: number, mi: number) => {
    setGroups((p) => p.map((g, i) => i === gi ? g.filter((_, j) => j !== mi) : g));
  };

  const save = async () => {
    const cleaned = groups.map((g) => g.filter((m) => m.trim())).filter((g) => g.length > 0);
    if (!cleaned.length) { setToast({ msg: t("至少需要一组值班人员"), type: "error" }); return; }
    if (!startDate) { setToast({ msg: t("请设置起始日期"), type: "error" }); return; }
    setSaving(true);
    try {
      await updateOncallSchedule(cleaned, startDate, username);
      setToast({ msg: t("值班表已保存"), type: "success" });
      setEditing(false); await load();
    } catch (e: any) { setToast({ msg: e.message, type: "error" }); }
    finally { setSaving(false); }
  };

  const currentGroupIdx = (() => {
    if (!startDate || groups.length === 0) return -1;
    const start = new Date(startDate);
    const today = new Date();
    const weeks = Math.floor((today.getTime() - start.getTime()) / (7 * 86400000));
    return weeks % groups.length;
  })();

  return (
    <div className="min-h-full">
      <header className="sticky top-0 z-10 backdrop-blur-md"
        style={{ background: "rgba(255,255,255,0.92)", borderBottom: `1px solid ${S.border}` }}>
        <div className="flex items-center justify-between px-6 py-3">
          <div>
            <h1 className="text-base font-semibold" style={{ color: S.text1 }}>{t("值班管理")}</h1>
            <p className="text-xs mt-0.5" style={{ color: S.text3 }}>{t("每周轮换，自动通知值班工程师")}</p>
          </div>
          {isAdmin && (
            <div className="flex items-center gap-2">
              {editing ? (
                <>
                  <button onClick={save} disabled={saving}
                    className="rounded-lg px-4 py-1.5 text-sm font-semibold disabled:opacity-50"
                    style={{ background: S.accent, color: "#0A0B0E" }}>
                    {saving ? t("保存中...") : t("保存")}
                  </button>
                  <button onClick={() => { setEditing(false); load(); }}
                    className="rounded-lg px-3 py-1.5 text-sm font-medium"
                    style={{ border: `1px solid ${S.border}`, color: S.text2 }}>
                    {t("取消")}
                  </button>
                </>
              ) : (
                <button onClick={() => setEditing(true)}
                  className="rounded-lg px-3 py-1.5 text-sm font-medium transition-colors"
                  style={{ border: `1px solid ${S.border}`, color: S.text2 }}>
                  {t("编辑排班")}
                </button>
              )}
            </div>
          )}
        </div>
      </header>

      <div className="mx-auto max-w-3xl px-6 py-6 space-y-5">
        {/* Current oncall */}
        <section className="rounded-xl p-5"
          style={{ background: "rgba(34,197,94,0.08)", border: "1px solid rgba(34,197,94,0.2)" }}>
          <div className="flex items-center gap-2 mb-3">
            <span className="h-2 w-2 rounded-full" style={{ background: "#16A34A", boxShadow: "0 0 6px rgba(34,197,94,0.5)" }} />
            <h2 className="text-sm font-semibold" style={{ color: "#16A34A" }}>{t("本周值班")}</h2>
          </div>
          {currentMembers.length > 0 ? (
            <div className="flex flex-wrap gap-2">
              {currentMembers.map((m) => (
                <span key={m} className="rounded-lg px-3 py-1.5 text-sm font-medium"
                  style={{ background: "rgba(34,197,94,0.12)", color: "#16A34A", border: "1px solid rgba(34,197,94,0.25)" }}>
                  {m}
                </span>
              ))}
            </div>
          ) : (
            <p className="text-sm" style={{ color: S.text3 }}>{t("尚未配置值班表")}</p>
          )}
        </section>

        {/* Start date */}
        <section className="rounded-xl p-5" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
          <h2 className="mb-3 text-sm font-semibold" style={{ color: S.text1 }}>{t("轮换起始日期")}</h2>
          {editing ? (
            <input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)}
              className="rounded-lg px-3 py-2 text-sm font-sans outline-none" style={inputStyle} />
          ) : (
            <p className="text-sm font-mono" style={{ color: S.text1 }}>{startDate || t("未设置")}</p>
          )}
          <p className="mt-1.5 text-xs" style={{ color: S.text3 }}>{t("从此日期开始，每周一轮换到下一组")}</p>
        </section>

        {/* Groups */}
        <section className="rounded-xl p-5" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
          <div className="mb-4 flex items-center justify-between">
            <h2 className="text-sm font-semibold" style={{ color: S.text1 }}>
              {t("值班分组")} <span style={{ color: S.text3 }}>({t("共")} {groups.length} {t("组")})</span>
            </h2>
            {editing && (
              <button onClick={addGroup}
                className="rounded-lg px-3 py-1 text-xs font-medium"
                style={{ background: S.accentBg, color: S.accent, border: "1px solid rgba(184,146,46,0.25)" }}>
                {t("添加分组")}
              </button>
            )}
          </div>

          {groups.length === 0 ? (
            <p className="py-8 text-center text-sm" style={{ color: S.text3 }}>{t("暂未配置值班分组")}</p>
          ) : (
            <div className="space-y-3">
              {groups.map((members, gi) => (
                <div key={gi} className="rounded-xl p-4"
                  style={gi === currentGroupIdx
                    ? { background: "rgba(34,197,94,0.06)", border: "1px solid rgba(34,197,94,0.2)" }
                    : { background: S.overlay, border: `1px solid ${S.border}` }}>
                  <div className="mb-2.5 flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-semibold" style={{ color: S.text1 }}>
                        {t("第")} {gi + 1} {t("组")}
                      </span>
                      {gi === currentGroupIdx && (
                        <span className="rounded-full px-2 py-0.5 text-[10px] font-bold"
                          style={{ background: "rgba(34,197,94,0.15)", color: "#16A34A" }}>
                          {t("本周")}
                        </span>
                      )}
                    </div>
                    {editing && groups.length > 1 && (
                      <button onClick={() => removeGroup(gi)} className="text-xs transition-colors"
                        style={{ color: "#DC2626" }}>
                        {t("删除分组")}
                      </button>
                    )}
                  </div>
                  <div className="space-y-2">
                    {members.map((email, mi) => (
                      <div key={mi} className="flex items-center gap-2">
                        {editing ? (
                          <>
                            <input value={email} onChange={(e) => updateMember(gi, mi, e.target.value)}
                              placeholder={t("飞书邮箱，如 engineer@plaud.ai")}
                              className="flex-1 rounded-lg px-3 py-1.5 text-sm font-sans outline-none"
                              style={inputStyle} />
                            <button onClick={() => removeMember(gi, mi)} className="text-xs transition-colors"
                              style={{ color: S.text3 }}>
                              {t("移除")}
                            </button>
                          </>
                        ) : (
                          <span className="text-sm" style={{ color: S.text2 }}>{email}</span>
                        )}
                      </div>
                    ))}
                    {editing && (
                      <button onClick={() => addMember(gi)} className="text-xs transition-colors"
                        style={{ color: "#2563EB" }}>
                        + {t("添加成员")}
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>

        {!isAdmin && (
          <p className="text-center text-xs" style={{ color: S.text3 }}>
            {t("只有管理员可以编辑值班排班")}
          </p>
        )}
      </div>

      {toast && <Toast msg={toast.msg} type={toast.type} onClose={() => setToast(null)} />}
    </div>
  );
}
