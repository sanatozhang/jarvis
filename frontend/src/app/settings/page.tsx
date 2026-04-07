"use client";

import { useT } from "@/lib/i18n";
import { useEffect, useState } from "react";
import { Toast } from "@/components/Toast";
import { fetchAgentConfig, fetchHealth, checkAgents, updateAgentConfig, fetchUsers, formatLocalTime, type AgentConfig, type HealthCheck, type UserListItem } from "@/lib/api";

interface EnvField { key: string; label: string; value: string; has_value: boolean; sensitive: boolean; }
interface EnvGroup { key: string; label: string; fields: EnvField[]; }

const S = {
  surface: "#F8F9FA", overlay: "#FFFFFF", hover: "#EEF0F2",
  border: "rgba(0,0,0,0.08)", borderSm: "rgba(0,0,0,0.04)",
  accent: "#B8922E", accentBg: "rgba(184,146,46,0.06)",
  text1: "#111827", text2: "#6B7280", text3: "#9CA3AF",
};

const inputStyle = {
  background: S.overlay,
  border: `1px solid ${S.border}`,
  color: S.text1,
  outline: "none",
};


function UserList() {
  const t = useT();
  const [users, setUsers] = useState<UserListItem[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchUsers().then(setUsers).catch(console.error).finally(() => setLoading(false));
  }, []);

  if (loading) return <p className="text-sm" style={{ color: S.text3 }}>{t("加载中...")}</p>;
  if (users.length === 0) return <p className="text-sm" style={{ color: S.text3 }}>{t("暂无数据")}</p>;

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr style={{ borderBottom: `1px solid ${S.border}` }}>
            {[t("用户名"), t("角色"), t("操作次数"), t("最后活跃"), t("注册时间")].map((h) => (
              <th key={h} className="pb-2 pr-4 text-left text-xs font-semibold uppercase tracking-wider"
                style={{ color: S.text3 }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {users.map((u) => (
            <tr key={u.username}
              style={{ borderBottom: `1px solid ${S.borderSm}` }}
              onMouseEnter={(e) => (e.currentTarget.style.background = S.hover + "60")}
              onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}>
              <td className="py-2.5 pr-4 font-medium" style={{ color: S.text1 }}>{u.username}</td>
              <td className="py-2.5 pr-4">
                <span className="rounded-full px-2 py-0.5 text-[10px] font-semibold"
                  style={u.role === "admin"
                    ? { background: S.accentBg, color: S.accent, border: "1px solid rgba(184,146,46,0.25)" }
                    : { background: S.overlay, color: S.text3, border: `1px solid ${S.border}` }}>
                  {u.role === "admin" ? t("管理员") : t("用户")}
                </span>
              </td>
              <td className="py-2.5 pr-4 tabular-nums font-mono text-xs" style={{ color: S.text2 }}>{u.action_count}</td>
              <td className="py-2.5 pr-4 font-mono text-xs" style={{ color: S.text3 }}>{formatLocalTime(u.last_active_at)}</td>
              <td className="py-2.5 font-mono text-xs" style={{ color: S.text3 }}>{formatLocalTime(u.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function SettingsPage() {
  const t = useT();
  const [config, setConfig] = useState<AgentConfig | null>(null);
  const [health, setHealth] = useState<HealthCheck | null>(null);
  const [agents, setAgents] = useState<Record<string, any>>({});
  const [toast, setToast] = useState("");
  const [saving, setSaving] = useState(false);

  const [envGroups, setEnvGroups] = useState<EnvGroup[]>([]);
  const [envEdits, setEnvEdits] = useState<Record<string, string>>({});
  const [envSaving, setEnvSaving] = useState(false);

  const username = typeof window !== "undefined" ? localStorage.getItem("appllo_username") || "" : "";
  const isAdmin = username === "sanato";

  useEffect(() => {
    fetchAgentConfig().then(setConfig).catch(console.error);
    fetchHealth().then(setHealth).catch(console.error);
    checkAgents().then(setAgents).catch(console.error);
    if (isAdmin) loadEnv();
  }, []);

  const loadEnv = async () => {
    try {
      const res = await fetch(`/api/env?username=${encodeURIComponent(username)}`);
      if (res.ok) {
        const data = await res.json();
        setEnvGroups(data.groups);
        const edits: Record<string, string> = {};
        for (const g of data.groups) {
          for (const f of g.fields) { edits[f.key] = f.value; }
        }
        setEnvEdits(edits);
      }
    } catch (e) { console.error(e); }
  };

  const saveAgentConfig = async () => {
    if (!config) return;
    setSaving(true);
    try {
      await updateAgentConfig({ default_agent: config.default, timeout: config.timeout, max_turns: config.max_turns, routing: config.routing });
      setToast(t("Agent 配置已保存"));
    } catch (e: any) { setToast(t("保存失败") + ": " + e.message); }
    finally { setSaving(false); }
  };

  const saveEnv = async () => {
    setEnvSaving(true);
    try {
      const res = await fetch(`/api/env?username=${encodeURIComponent(username)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ updates: envEdits }),
      });
      const data = await res.json();
      if (data.status === "no_changes") {
        setToast(t("没有需要保存的更改"));
      } else {
        setToast(`${t("已保存")}: ${data.keys?.join(", ")}${data.note ? `. ${data.note}` : ""}`);
        loadEnv();
      }
    } catch (e: any) { setToast(t("保存失败") + ": " + e.message); }
    finally { setEnvSaving(false); }
  };

  const ruleTypes = ["recording_missing", "timestamp_drift", "bluetooth", "cloud_sync", "speaker", "flutter_crash", "file_transfer", "membership_payment", "hardware_firmware", "general"];

  const statusColor = (s: string) =>
    s === "ok" || s === "healthy" ? "#16A34A" :
    s === "unavailable" ? "#CA8A04" : "#DC2626";

  return (
    <div className="min-h-full">
      {/* Header */}
      <header className="sticky top-0 z-10 backdrop-blur-md"
        style={{ background: "rgba(255,255,255,0.92)", borderBottom: `1px solid ${S.border}` }}>
        <div className="flex items-center justify-between px-6 py-3">
          <div>
            <h1 className="text-base font-semibold" style={{ color: S.text1 }}>{t("系统设置")}</h1>
            <p className="text-xs mt-0.5" style={{ color: S.text3 }}>{t("Agent 配置、环境变量与系统状态")}</p>
          </div>
          <button onClick={saveAgentConfig} disabled={saving}
            className="rounded-lg px-4 py-1.5 text-sm font-semibold disabled:opacity-50 transition-opacity"
            style={{ background: S.accent, color: "#0A0B0E" }}>
            {saving ? t("保存中...") : t("保存 Agent 配置")}
          </button>
        </div>
      </header>

      <div className="mx-auto max-w-3xl px-6 py-6 space-y-5">

        {/* ENV SETTINGS (Admin only) */}
        {isAdmin && envGroups.length > 0 && (
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <div>
                <h2 className="text-sm font-semibold" style={{ color: S.text1 }}>{t("环境配置")}</h2>
                <p className="text-xs mt-0.5" style={{ color: S.text3 }}>{t("修改后需要重启服务才能完全生效")}</p>
              </div>
              <button onClick={saveEnv} disabled={envSaving}
                className="rounded-lg px-4 py-1.5 text-sm font-semibold disabled:opacity-50 transition-opacity"
                style={{ background: S.overlay, color: S.text1, border: `1px solid ${S.border}` }}>
                {envSaving ? t("保存中...") : t("保存环境配置")}
              </button>
            </div>

            {envGroups.map((group) => (
              <section key={group.key} className="rounded-xl p-5"
                style={{ background: S.surface, border: `1px solid ${S.border}` }}>
                <h3 className="mb-4 text-xs font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>
                  {group.label}
                </h3>
                <div className="space-y-4">
                  {group.fields.map((field) => (
                    <div key={field.key}>
                      <label className="mb-1.5 flex items-center gap-2 text-xs font-medium" style={{ color: S.text2 }}>
                        {field.label}
                        <code className="rounded px-1.5 py-0.5 font-mono text-[10px]"
                          style={{ background: S.overlay, color: S.text3 }}>
                          {field.key}
                        </code>
                        {field.sensitive && (
                          <span className="rounded px-1.5 py-0.5 text-[9px] font-semibold"
                            style={{ background: S.accentBg, color: S.accent }}>
                            {t("敏感")}
                          </span>
                        )}
                        {field.has_value && (
                          <span className="h-1.5 w-1.5 rounded-full" style={{ background: "#16A34A" }} title={t("已配置")} />
                        )}
                      </label>
                      <input
                        type={field.sensitive ? "password" : "text"}
                        value={envEdits[field.key] || ""}
                        onChange={(e) => setEnvEdits((p) => ({ ...p, [field.key]: e.target.value }))}
                        onFocus={(e) => {
                          if (field.sensitive && e.target.value.includes("••••")) {
                            setEnvEdits((p) => ({ ...p, [field.key]: "" }));
                          }
                        }}
                        placeholder={field.sensitive ? t("输入新值以更新") : t("未设置")}
                        className="w-full rounded-lg px-3 py-2 font-mono text-sm transition-colors"
                        style={inputStyle}
                      />
                    </div>
                  ))}
                </div>
              </section>
            ))}
          </div>
        )}

        {!isAdmin && (
          <div className="rounded-xl p-4 text-sm"
            style={{ background: S.accentBg, border: "1px solid rgba(184,146,46,0.2)", color: S.text2 }}>
            {t("环境配置仅管理员可见")}。{t("当前用户")}: <span style={{ color: S.text1 }}>{username || t("未登录")}</span>
          </div>
        )}

        {/* SYSTEM HEALTH */}
        <section className="rounded-xl p-5" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
          <h2 className="mb-4 text-xs font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>{t("系统状态")}</h2>
          {!health ? (
            <p className="text-sm" style={{ color: S.text3 }}>{t("检查中...")}</p>
          ) : (
            <div className="grid grid-cols-2 gap-2.5">
              <div className="flex items-center gap-2.5 rounded-lg px-3 py-2.5"
                style={{ background: S.overlay, border: `1px solid ${S.border}` }}>
                <span className="h-2.5 w-2.5 flex-shrink-0 rounded-full"
                  style={{ background: statusColor(health.status), boxShadow: `0 0 6px ${statusColor(health.status)}60` }} />
                <span className="text-sm" style={{ color: S.text2 }}>
                  {t("整体")}: <span style={{ color: S.text1, fontWeight: 500 }}>{health.status}</span>
                </span>
              </div>
              {health.checks && Object.entries(health.checks).map(([key, val]: [string, any]) =>
                key !== "agents" && (
                  <div key={key} className="flex items-center gap-2.5 rounded-lg px-3 py-2.5"
                    style={{ background: S.overlay, border: `1px solid ${S.border}` }}>
                    <span className="h-2 w-2 flex-shrink-0 rounded-full"
                      style={{ background: statusColor(val.status) }} />
                    <span className="text-sm truncate" style={{ color: S.text2 }}>
                      {key}: <span style={{ color: S.text1 }}>{val.status}</span>
                      {val.note && <span style={{ color: S.text3 }}> ({val.note})</span>}
                    </span>
                  </div>
                )
              )}
            </div>
          )}
        </section>

        {/* AGENT AVAILABILITY */}
        <section className="rounded-xl p-5" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
          <h2 className="mb-4 text-xs font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>{t("Agent 可用性")}</h2>
          <div className="space-y-2">
            {Object.entries(agents).map(([name, info]: [string, any]) => (
              <div key={name} className="flex items-center justify-between rounded-lg px-4 py-2.5"
                style={{ background: S.overlay, border: `1px solid ${S.border}` }}>
                <div className="flex items-center gap-2.5">
                  <span className="h-2.5 w-2.5 rounded-full flex-shrink-0"
                    style={{
                      background: info.available ? "#16A34A" : "#DC2626",
                      boxShadow: info.available ? "0 0 6px rgba(34,197,94,0.4)" : "none",
                    }} />
                  <span className="text-sm font-medium" style={{ color: S.text1 }}>{name}</span>
                </div>
                <span className="text-xs font-mono" style={{ color: S.text3 }}>
                  {info.available ? (info.version || t("已安装")) : (info.error || t("未安装"))}
                </span>
              </div>
            ))}
          </div>
        </section>

        {/* AGENT CONFIGURATION */}
        {config && (
          <>
            <section className="rounded-xl p-5" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
              <h2 className="mb-4 text-xs font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>{t("Agent 配置")}</h2>
              <div className="grid grid-cols-3 gap-4">
                <div>
                  <label className="mb-1.5 block text-xs font-medium" style={{ color: S.text2 }}>{t("默认 Agent")}</label>
                  <select value={config.default}
                    onChange={(e) => {
                      const newAgent = e.target.value;
                      const newRouting = Object.fromEntries(Object.keys(config.routing).map((k) => [k, newAgent]));
                      setConfig({ ...config, default: newAgent, routing: newRouting });
                    }}
                    className="w-full rounded-lg px-3 py-2 text-sm outline-none font-sans"
                    style={inputStyle}>
                    {Object.keys(config.providers).map((p) => <option key={p} value={p}>{p}</option>)}
                  </select>
                </div>
                <div>
                  <label className="mb-1.5 block text-xs font-medium" style={{ color: S.text2 }}>{t("超时（秒）")}</label>
                  <input type="number" value={config.timeout}
                    onChange={(e) => setConfig({ ...config, timeout: parseInt(e.target.value) || 300 })}
                    className="w-full rounded-lg px-3 py-2 text-sm font-mono"
                    style={inputStyle} />
                </div>
                <div>
                  <label className="mb-1.5 block text-xs font-medium" style={{ color: S.text2 }}>{t("最大轮数")}</label>
                  <input type="number" value={config.max_turns}
                    onChange={(e) => setConfig({ ...config, max_turns: parseInt(e.target.value) || 25 })}
                    className="w-full rounded-lg px-3 py-2 text-sm font-mono"
                    style={inputStyle} />
                </div>
              </div>
            </section>

            <section className="rounded-xl p-5" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
              <h2 className="mb-4 text-xs font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>
                {t("问题类型 → Agent 路由")}
              </h2>
              <div className="space-y-1.5">
                {ruleTypes.map((rt) => (
                  <div key={rt} className="flex items-center justify-between rounded-lg px-4 py-2"
                    style={{ background: S.overlay }}
                    onMouseEnter={(e) => (e.currentTarget.style.background = S.hover)}
                    onMouseLeave={(e) => (e.currentTarget.style.background = S.overlay)}>
                    <span className="text-sm font-mono" style={{ color: S.text2 }}>{rt}</span>
                    <select
                      value={config.routing[rt] || config.default}
                      onChange={(e) => setConfig({ ...config, routing: { ...config.routing, [rt]: e.target.value } })}
                      className="rounded-md px-2 py-1 text-xs font-sans outline-none"
                      style={{ background: "#F8F9FA", border: `1px solid ${S.border}`, color: S.text1 }}>
                      {Object.keys(config.providers).map((p) => <option key={p} value={p}>{p}</option>)}
                    </select>
                  </div>
                ))}
              </div>
            </section>
          </>
        )}

        {/* USER MANAGEMENT (Admin only) */}
        {isAdmin && (
          <section className="rounded-xl p-5" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
            <h2 className="mb-4 text-xs font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>
              {t("用户管理")}
            </h2>
            <UserList />
          </section>
        )}
      </div>

      {toast && <Toast msg={toast} onClose={() => setToast("")} />}
    </div>
  );
}
