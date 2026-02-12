"use client";

import { useEffect, useState } from "react";
import { fetchAgentConfig, fetchHealth, checkAgents, updateAgentConfig, type AgentConfig, type HealthCheck } from "@/lib/api";

export default function SettingsPage() {
  const [config, setConfig] = useState<AgentConfig | null>(null);
  const [health, setHealth] = useState<HealthCheck | null>(null);
  const [agents, setAgents] = useState<Record<string, any>>({});
  const [toast, setToast] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    fetchAgentConfig().then(setConfig).catch(console.error);
    fetchHealth().then(setHealth).catch(console.error);
    checkAgents().then(setAgents).catch(console.error);
  }, []);

  const save = async () => {
    if (!config) return;
    setSaving(true);
    try {
      await updateAgentConfig({
        default_agent: config.default,
        timeout: config.timeout,
        max_turns: config.max_turns,
        routing: config.routing,
      });
      setToast("配置已保存");
    } catch (e: any) {
      setToast("保存失败: " + e.message);
    } finally {
      setSaving(false);
      setTimeout(() => setToast(""), 2000);
    }
  };

  const ruleTypes = ["recording_missing", "timestamp_drift", "bluetooth", "cloud_sync", "speaker", "flutter_crash", "general"];

  return (
    <div className="min-h-full">
      <header className="sticky top-0 z-10 border-b border-gray-200 bg-white/80 backdrop-blur-sm">
        <div className="flex items-center justify-between px-6 py-3">
          <h1 className="text-lg font-semibold">系统设置</h1>
          <button
            onClick={save}
            disabled={saving}
            className="rounded-lg bg-black px-4 py-1.5 text-sm font-medium text-white hover:bg-gray-800 disabled:opacity-50"
          >
            {saving ? "保存中..." : "保存配置"}
          </button>
        </div>
      </header>

      <div className="mx-auto max-w-3xl px-6 py-6 space-y-6">
        {/* System Health */}
        <section className="rounded-xl border border-gray-100 bg-white p-5">
          <h2 className="mb-3 text-sm font-semibold">系统状态</h2>
          {!health ? (
            <p className="text-sm text-gray-300">检查中...</p>
          ) : (
            <div className="grid grid-cols-2 gap-3">
              <div className="flex items-center gap-2">
                <span className={`h-2.5 w-2.5 rounded-full ${health.status === "healthy" ? "bg-green-400" : "bg-yellow-400"}`} />
                <span className="text-sm text-gray-600">
                  整体: <span className="font-medium">{health.status}</span>
                </span>
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
                <span className="text-xs text-gray-400">
                  {info.available ? (info.version || "已安装") : (info.error || "未安装")}
                </span>
              </div>
            ))}
            {Object.keys(agents).length === 0 && <p className="text-sm text-gray-300">检查中...</p>}
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
                  <select
                    value={config.default}
                    onChange={(e) => setConfig({ ...config, default: e.target.value })}
                    className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm outline-none focus:border-black"
                  >
                    {Object.keys(config.providers).map((p) => (
                      <option key={p} value={p}>{p}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="mb-1 block text-xs font-medium text-gray-500">超时（秒）</label>
                  <input
                    type="number"
                    value={config.timeout}
                    onChange={(e) => setConfig({ ...config, timeout: parseInt(e.target.value) || 300 })}
                    className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm outline-none focus:border-black"
                  />
                </div>
                <div>
                  <label className="mb-1 block text-xs font-medium text-gray-500">最大轮数</label>
                  <input
                    type="number"
                    value={config.max_turns}
                    onChange={(e) => setConfig({ ...config, max_turns: parseInt(e.target.value) || 25 })}
                    className="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm outline-none focus:border-black"
                  />
                </div>
              </div>
            </section>

            {/* Provider details */}
            <section className="rounded-xl border border-gray-100 bg-white p-5">
              <h2 className="mb-3 text-sm font-semibold">Provider 详情</h2>
              <div className="space-y-2">
                {Object.entries(config.providers).map(([name, p]: [string, any]) => (
                  <div key={name} className="rounded-lg bg-gray-50 px-4 py-3">
                    <div className="flex items-center justify-between">
                      <span className="text-sm font-medium text-gray-700">{name}</span>
                      <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${
                        p.enabled ? "bg-green-50 text-green-600" : "bg-gray-200 text-gray-500"
                      }`}>
                        {p.enabled ? "启用" : "禁用"}
                      </span>
                    </div>
                    <p className="mt-1 text-xs text-gray-400">
                      模型: {p.model || "默认"} · 超时: {p.timeout || config.timeout}s
                    </p>
                  </div>
                ))}
              </div>
            </section>

            {/* Routing */}
            <section className="rounded-xl border border-gray-100 bg-white p-5">
              <h2 className="mb-3 text-sm font-semibold">问题类型 → Agent 路由</h2>
              <div className="space-y-2">
                {ruleTypes.map((rt) => (
                  <div key={rt} className="flex items-center justify-between rounded-lg bg-gray-50 px-4 py-2">
                    <span className="text-sm text-gray-600">{rt}</span>
                    <select
                      value={config.routing[rt] || config.default}
                      onChange={(e) => setConfig({ ...config, routing: { ...config.routing, [rt]: e.target.value } })}
                      className="rounded-md border border-gray-200 px-2 py-1 text-xs outline-none"
                    >
                      {Object.keys(config.providers).map((p) => (
                        <option key={p} value={p}>{p}</option>
                      ))}
                    </select>
                  </div>
                ))}
              </div>
            </section>
          </>
        )}
      </div>

      {toast && (
        <div className="fixed bottom-6 right-6 z-50 rounded-lg bg-gray-900 px-4 py-2.5 text-sm font-medium text-white shadow-lg">
          {toast}
        </div>
      )}
    </div>
  );
}
