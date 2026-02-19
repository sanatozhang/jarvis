"use client";

import { useT } from "@/lib/i18n";

import { useEffect, useState } from "react";
import { getOncallSchedule, getOncallCurrent, updateOncallSchedule, type OncallGroup } from "@/lib/api";

function Toast({ msg, type, onClose }: { msg: string; type: "success" | "error"; onClose: () => void }) {
  useEffect(() => { const t = setTimeout(onClose, 3000); return () => clearTimeout(t); }, [onClose]);
  return <div className={`fixed bottom-6 right-6 z-50 rounded-lg px-4 py-2.5 text-sm font-medium text-white shadow-lg ${type === "success" ? "bg-green-600" : "bg-red-600"}`}>{msg}</div>;
}

export default function OncallPage() {
  const t = useT();
  const [groups, setGroups] = useState<string[][]>([]);
  const [startDate, setStartDate] = useState("");
  const [currentMembers, setCurrentMembers] = useState<string[]>([]);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState<{ msg: string; type: "success" | "error" } | null>(null);

  const username = typeof window !== "undefined" ? localStorage.getItem("jarvis_username") || "" : "";
  const isAdmin = username === "sanato"; // matches backend ADMIN_USERNAME

  const load = async () => {
    try {
      const [sched, curr] = await Promise.all([getOncallSchedule(), getOncallCurrent()]);
      setGroups(sched.groups.map((g) => g.members));
      setStartDate(sched.start_date || new Date().toISOString().slice(0, 10));
      setCurrentMembers(curr.members);
    } catch (e: any) {
      setToast({ msg: e.message, type: "error" });
    }
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
      setEditing(false);
      await load();
    } catch (e: any) {
      setToast({ msg: e.message, type: "error" });
    } finally {
      setSaving(false);
    }
  };

  // Calculate which group is current
  const currentGroupIdx = (() => {
    if (!startDate || groups.length === 0) return -1;
    const start = new Date(startDate);
    const today = new Date();
    const weeks = Math.floor((today.getTime() - start.getTime()) / (7 * 86400000));
    return weeks % groups.length;
  })();

  return (
    <div className="min-h-full">
      <header className="sticky top-0 z-10 border-b border-gray-200 bg-white/80 backdrop-blur-sm">
        <div className="flex items-center justify-between px-6 py-3">
          <div>
            <h1 className="text-lg font-semibold">{t("值班管理")}</h1>
            <p className="text-xs text-gray-400">{t("每周轮换，自动通知值班工程师")}</p>
          </div>
          {isAdmin && (
            <div className="flex items-center gap-2">
              {editing ? (
                <>
                  <button onClick={save} disabled={saving} className="rounded-lg bg-black px-4 py-1.5 text-sm font-medium text-white hover:bg-gray-800 disabled:opacity-50">{saving ? t("保存中...") : t("保存")}</button>
                  <button onClick={() => { setEditing(false); load(); }} className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm font-medium text-gray-600 hover:bg-gray-50">{t("取消")}</button>
                </>
              ) : (
                <button onClick={() => setEditing(true)} className="rounded-lg border border-gray-200 px-3 py-1.5 text-sm font-medium text-gray-600 hover:bg-gray-50">{t("编辑排班")}</button>
              )}
            </div>
          )}
        </div>
      </header>

      <div className="mx-auto max-w-3xl px-6 py-6 space-y-6">
        {/* Current oncall */}
        <section className="rounded-xl border border-green-200 bg-green-50/30 p-5">
          <h2 className="mb-2 text-sm font-semibold text-green-800">{t("本周值班")}</h2>
          {currentMembers.length > 0 ? (
            <div className="flex flex-wrap gap-2">
              {currentMembers.map((m) => (
                <span key={m} className="rounded-lg bg-green-100 px-3 py-1.5 text-sm font-medium text-green-700">{m}</span>
              ))}
            </div>
          ) : (
            <p className="text-sm text-gray-400">{t("尚未配置值班表")}</p>
          )}
        </section>

        {/* Start date */}
        <section className="rounded-xl border border-gray-100 bg-white p-5">
          <h2 className="mb-3 text-sm font-semibold">{t("轮换起始日期")}</h2>
          {editing ? (
            <input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} className="rounded-lg border border-gray-200 px-3 py-2 text-sm outline-none focus:border-black" />
          ) : (
            <p className="text-sm text-gray-600">{startDate || t("未设置")}</p>
          )}
          <p className="mt-1 text-xs text-gray-400">{t("从此日期开始，每周一轮换到下一组")}</p>
        </section>

        {/* Groups */}
        <section className="rounded-xl border border-gray-100 bg-white p-5">
          <div className="mb-4 flex items-center justify-between">
            <h2 className="text-sm font-semibold">{t("值班分组")}（{t("共")} {groups.length} {t("组")}）</h2>
            {editing && (
              <button onClick={addGroup} className="rounded-md bg-black px-3 py-1 text-xs font-medium text-white hover:bg-gray-800">{t("添加分组")}</button>
            )}
          </div>

          {groups.length === 0 ? (
            <p className="py-8 text-center text-sm text-gray-300">{t("暂未配置值班分组")}</p>
          ) : (
            <div className="space-y-3">
              {groups.map((members, gi) => (
                <div key={gi} className={`rounded-lg border p-4 ${gi === currentGroupIdx ? "border-green-300 bg-green-50/30" : "border-gray-100 bg-gray-50/50"}`}>
                  <div className="mb-2 flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium text-gray-700">{t("第")} {gi + 1} {t("组")}</span>
                      {gi === currentGroupIdx && <span className="rounded-full bg-green-100 px-2 py-0.5 text-[10px] font-semibold text-green-700">{t("本周")}</span>}
                    </div>
                    {editing && groups.length > 1 && (
                      <button onClick={() => removeGroup(gi)} className="text-xs text-red-400 hover:text-red-600">{t("删除分组")}</button>
                    )}
                  </div>
                  <div className="space-y-2">
                    {members.map((email, mi) => (
                      <div key={mi} className="flex items-center gap-2">
                        {editing ? (
                          <>
                            <input
                              value={email}
                              onChange={(e) => updateMember(gi, mi, e.target.value)}
                              placeholder={t("飞书邮箱，如 engineer@plaud.ai")}
                              className="flex-1 rounded-md border border-gray-200 px-3 py-1.5 text-sm outline-none focus:border-black"
                            />
                            <button onClick={() => removeMember(gi, mi)} className="text-xs text-gray-400 hover:text-red-500">{t("移除")}</button>
                          </>
                        ) : (
                          <span className="text-sm text-gray-600">{email}</span>
                        )}
                      </div>
                    ))}
                    {editing && (
                      <button onClick={() => addMember(gi)} className="text-xs text-blue-500 hover:text-blue-700">{t("添加成员")}</button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>

        {!isAdmin && (
          <p className="text-center text-xs text-gray-400">{t("只有管理员可以编辑值班排班")}</p>
        )}
      </div>

      {toast && <Toast msg={toast.msg} type={toast.type} onClose={() => setToast(null)} />}
    </div>
  );
}
