"use client";

import { useEffect, useState } from "react";
import { fetchAgentConfig, fetchHealth, checkAgents, updateAgentConfig, type AgentConfig, type HealthCheck } from "@/lib/api";

interface EnvField { key: string; label: string; value: string; has_value: boolean; sensitive: boolean; }
interface EnvGroup { key: string; label: string; fields: EnvField[]; }

function Toast({ msg, onClose }: { msg: string; onClose: () => void }) {
  useEffect(() => { const t = setTimeout(onClose, 3000); return () => clearTimeout(t); }, [onClose]);
  return <div className="fixed bottom-6 right-6 z-50 rounded-lg bg-gray-900 px-4 py-2.5 text-sm font-medium text-white shadow-lg">{msg}</div>;
}

export default function SettingsPage() {
  const [config, setConfig] = useState<AgentConfig | null>(null);
  const [health, setHealth] = useState<HealthCheck | null>(null);
  const [agents, setAgents] = useState<Record<string, any>>({});
  const [toast, setToast] = useState("");
  const [saving, setSaving] = useState(false);

  // Env settings
  const [envGroups, setEnvGroups] = useState<EnvGroup[]>([]);
  const [envEdits, setEnvEdits] = useState<Record<string, string>>({});
  const [envSaving, setEnvSaving] = useState(false);

  const username = typeof window !== "undefined" ? localStorage.getItem("jarvis_username") || "" : "";
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
        // Initialize edits from current values
        const edits: Record<string, string> = {};
        for (const g of data.groups) {
          for (const f of g.fields) {
            edits[f.key] = f.value;
          }
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
      setToast("Agent 配置已保存");
    } catch (e: any) { setToast("保存失败: " + e.message); }
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
        setToast("没有需要保存的更改");
      } else {
        setToast(`已保存: ${data.keys?.join(", ")}。${data.note || ""}`);
        loadEnv(); // reload to get fresh masked values
      }
    } catch (e: any) { setToast("保存失败: " + e.message); }
    finally { setEnvSaving(false); }
  };

  const ruleTypes = ["recording_missing", "timestamp_drift", "bluetooth", "cloud_sync", "speaker", "flutter_crash", "file_transfer", "membership_payment", "hardware_firmware", "general"];

  return (
    <div className="min-h-full">
      <header className="sticky top-0 z-10 border-b border-gray-200 bg-white/80 backdrop-blur-sm">
        <div className="flex items-center justify-between px-6 py-3">
          <h1 className="text-lg font-semibold">系统设置</h1>
          <div className="flex items-center gap-2">
            <button onClick={saveAgentConfig} disabled={saving} className="rounded-lg bg-black px-4 py-1.5 text-sm font-medium text-white hover:bg-gray-800 disabled:opacity-50">
              {saving ? "保存中..." : "保存 Agent 配置"}
            </button>
          </div>
        </div>
      </header>

      <div className="mx-auto max-w-3xl px-6 py-6 space-y-6">

        {/* ============ ENV SETTINGS (Admin only) ============ */}
        {isAdmin && envGroups.length > 0 && (
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <h2 className="text-base font-semibold text-gray-800">环境配置</h2>
              <button onClick={saveEnv} disabled={envSaving} className="rounded-lg bg-black px-4 py-1.5 text-sm font-medium text-white hover:bg-gray-800 disabled:opacity-50">
                {envSaving ? "保存中..." : "保存环境配置"}
              </button>
            </div>
            <p className="text-xs text-gray-400">修改后需要重启服务才能完全生效。敏感字段显示为掩码，清空后重新输入即可更新。</p>

            {envGroups.map((group) => (
              <section key={group.key} className="rounded-xl border border-gray-100 bg-white p-5">
                <h3 className="mb-3 text-sm font-semibold text-gray-700">{group.label}</h3>
                <div className="space-y-3">
                  {group.fields.map((field) => (
                    <div key={field.key}>
                      <label className="mb-1 flex items-center gap-2 text-xs font-medium text-gray-500">
                        {field.label}
                        <code className="rounded bg-gray-100 px-1 py-0.5 text-[10px] text-gray-400">{field.key}</code>
                        {field.sensitive && <span className="rounded bg-amber-50 px-1 py-0.5 text-[9px] text-amber-600">敏感</span>}
                        {field.has_value && <span className="h-1.5 w-1.5 rounded-full bg-green-400" title="已配置" />}
                      </label>
                      <input
                        type={field.sensitive ? "password" : "text"}
                        value={envEdits[field.key] || ""}
                        onChange={(e) => setEnvEdits((p) => ({ ...p, [field.key]: e.target.value }))}
                        onFocus={(e) => {
                          // Clear masked value on focus so user can type new value
                          if (field.sensitive && e.target.value.includes("••••")) {
                            setEnvEdits((p) => ({ ...p, [field.key]: "" }));
                          }
                        }}
                        placeholder={field.sensitive ? "输入新值以更新" : "未设置"}
                        className="w-full rounded-lg border border-gray-200 px-3 py-2 font-mono text-sm text-gray-700 outline-none transition-colors focus:border-black"
                      />
                    </div>
                  ))}
                </div>
              </section>
            ))}
          </div>
        )}

        {!isAdmin && (
          <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-700">
            环境配置仅管理员可见。当前用户: {username || "未登录"}
          </div>
        )}

        <hr className="border-gray-100" />

        {/* ============ SYSTEM HEALTH ============ */}
        <section className="rounded-xl border border-gray-100 bg-white p-5">
          <h2 className="mb-3 text-sm font-semibold">系统状态</h2>
          {!health ? <p className="text-sm text-gray-300">检查中...</p> : (
            <div className="grid grid-cols-2 gap-3">
              <div className="flex items-center gap-2">
                <span className={`h-2.5 w-2.5 rounded-full ${health.status === "healthy" ? "bg-green-400" : "bg-yellow-400"}`} />
                <span className="text-sm text-gray-600">整体: <span className="font-medium">{health.status}</span></span>
              </div>
              {health.checks && Object.entries(health.checks).map(([key, val]: [string, any]) => (
                key !== "agents" && (
                  <div key={key} className="flex items-center gap-2">
                    <span className={`h-2 w-2 rounded-full ${val.status === "ok" ? "bg-green-400" : val.status === "unavailable" ? "bg-yellow-400" : "bg-red-400"}`} />
                    <span className="text-sm text-gray-500">{key}: {val.status} {val.note ? `(${val.note})` : ""}</span>
                  </div>
                )
              ))}
            </div>
          )}
        </section>

        {/* Agent Availability */}
        <section className="rounded-xl border border-gray-100 bg-white p-5">
          <h2 className="mb-3 text-sm font-semibold">Agent 可用性</h2>
          <div className="space-y-2">
            {Object.entries(agents).map(([name, info]: [string, any]) => (
              <div key={name} className="flex items-center justify-between rounded-lg bg-gray-50 px-4 py-2.5">
                <div className="flex items-center gap-2">
                  <span className={`h-2.5 w-2.5 rounded-full ${info.available ? "bg-green-400" : "bg-red-400"}`} />
                  <span className="text-sm font-medium text-gray-700">{name}</span>
                </div>
                <span className="text-xs text-gray-400">{info.available ? (info.version || "已安装") : (info.error || "未安装")}</span>
              </div>
            ))}
          </div>
        </section>

        {/* Agent Configuration */}
        {config && (
          <>
            <section className="rounded-xl border border-gray-100 bg-white p-5">
              <h2 className="mb-4 text-sm font-semibold">Agent 配置</h2>
              <div className="grid grid-cols-3 gap-4">
                <div>
                  <label className="mb-1 block text-xs font-medium text-gray-500">默认 Agent</label>
                  <select value={config.default} onChange={(e) => setConfig({ ...config, default: e.target.value })} className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm outline-none focus:border-black">
                    {Object.keys(config.providers).map((p) => <option key={p} value={p}>{p}</option>)}
                  </select>
                </div>
                <div>
                  <label className="mb-1 block text-xs font-medium text-gray-500">超时（秒）</label>
                  <input type="number" value={config.timeout} onChange={(e) => setConfig({ ...config, timeout: parseInt(e.target.value) || 300 })} className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm outline-none focus:border-black" />
                </div>
                <div>
                  <label className="mb-1 block text-xs font-medium text-gray-500">最大轮数</label>
                  <input type="number" value={config.max_turns} onChange={(e) => setConfig({ ...config, max_turns: parseInt(e.target.value) || 25 })} className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm outline-none focus:border-black" />
                </div>
              </div>
            </section>

            <section className="rounded-xl border border-gray-100 bg-white p-5">
              <h2 className="mb-3 text-sm font-semibold">问题类型 → Agent 路由</h2>
              <div className="space-y-2">
                {ruleTypes.map((rt) => (
                  <div key={rt} className="flex items-center justify-between rounded-lg bg-gray-50 px-4 py-2">
                    <span className="text-sm text-gray-600">{rt}</span>
                    <select value={config.routing[rt] || config.default} onChange={(e) => setConfig({ ...config, routing: { ...config.routing, [rt]: e.target.value } })} className="rounded-md border border-gray-200 px-2 py-1 text-xs outline-none">
                      {Object.keys(config.providers).map((p) => <option key={p} value={p}>{p}</option>)}
                    </select>
                  </div>
                ))}
              </div>
            </section>
          </>
        )}
      </div>

      {toast && <Toast msg={toast} onClose={() => setToast("")} />}
    </div>
  );
}
