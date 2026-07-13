"use client";

import { useT } from "@/lib/i18n";
import { useEffect, useState } from "react";
import { Toast } from "@/components/Toast";
import { fetchAgentConfig, fetchHealth, checkAgents, updateAgentConfig, fetchUsers, formatLocalTime, fetchEscalationMembers, updateEscalationMembers, fetchCondensationConfig, updateCondensationConfig, fetchAutoDeepAnalysisConfig, updateAutoDeepAnalysisConfig, fetchSymbolSettings, updateSymbolSettings, getRepoRouting, updateRepoRouting, previewRepoRouting, type AgentConfig, type HealthCheck, type UserListItem, type CondensationConfig, type AutoDeepAnalysisConfig, type SymbolSettings, type RepoBand, type RepoRoutingConfig, type RepoRoutingPreviewResult } from "@/lib/api";
import { getBatchTopN, setBatchTopN, BATCH_TOP_N_BOUNDS } from "@/lib/crashguard-prefs";

interface EnvField { key: string; label: string; value: string; has_value: boolean; sensitive: boolean; }
interface EnvGroup { key: string; label: string; fields: EnvField[]; }

const S = {
  surface: "var(--j-surface)", overlay: "var(--j-panel)", hover: "var(--j-hover)",
  border: "var(--j-border)", borderSm: "var(--j-border-sm)",
  accent: "var(--j-accent)", accentBg: "var(--j-accent-soft)",
  text1: "var(--j-ink)", text2: "var(--j-graphite)", text3: "var(--j-faint)",
};

const inputStyle = {
  background: S.overlay,
  border: `1px solid ${S.border}`,
  color: S.text1,
  outline: "none",
};


function CrashguardPrefsSection() {
  const t = useT();
  // 批量分析 Top N（localStorage）
  const [n, setN] = useState<number>(BATCH_TOP_N_BOUNDS.default);
  const [draft, setDraft] = useState<string>(String(BATCH_TOP_N_BOUNDS.default));
  const [saved, setSaved] = useState(false);

  // 符号化设置（服务端）
  const [symSettings, setSymSettings] = useState<SymbolSettings>({ symbol_upload_keep_versions: 10, github_cache_keep_versions: 10 });
  const [symDraft, setSymDraft] = useState({ symbol_upload_keep_versions: "10", github_cache_keep_versions: "10" });
  const [symSaving, setSymSaving] = useState(false);
  const [symSaved, setSymSaved] = useState(false);
  const [symError, setSymError] = useState("");

  useEffect(() => {
    const cur = getBatchTopN();
    setN(cur);
    setDraft(String(cur));
    fetchSymbolSettings().then((s) => {
      setSymSettings(s);
      setSymDraft({ symbol_upload_keep_versions: String(s.symbol_upload_keep_versions), github_cache_keep_versions: String(s.github_cache_keep_versions) });
    }).catch(() => {});
  }, []);

  const onSave = () => {
    const parsed = parseInt(draft, 10);
    const applied = setBatchTopN(Number.isFinite(parsed) ? parsed : BATCH_TOP_N_BOUNDS.default);
    setN(applied);
    setDraft(String(applied));
    setSaved(true);
    setTimeout(() => setSaved(false), 1500);
  };

  const onSaveSymbols = async () => {
    setSymSaving(true);
    setSymError("");
    const upload = parseInt(symDraft.symbol_upload_keep_versions, 10);
    const github = parseInt(symDraft.github_cache_keep_versions, 10);
    if (!Number.isFinite(upload) || upload < 1 || upload > 50 || !Number.isFinite(github) || github < 1 || github > 50) {
      setSymError(t("请输入 1–50 的整数"));
      setSymSaving(false);
      return;
    }
    try {
      const result = await updateSymbolSettings({ symbol_upload_keep_versions: upload, github_cache_keep_versions: github });
      setSymSettings(result);
      setSymDraft({ symbol_upload_keep_versions: String(result.symbol_upload_keep_versions), github_cache_keep_versions: String(result.github_cache_keep_versions) });
      setSymSaved(true);
      setTimeout(() => setSymSaved(false), 2000);
    } catch {
      setSymError(t("保存失败，请重试"));
    } finally {
      setSymSaving(false);
    }
  };

  return (
    <section className="rounded-xl p-5" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
      <h2 className="mb-4 text-xs font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>
        Crashguard
      </h2>
      <div className="flex flex-col gap-4 max-w-md">
        {/* 批量分析 Top N */}
        <div>
          <label className="block text-sm mb-1" style={{ color: S.text1 }}>
            {t("批量分析 Top N")}
          </label>
          <p className="text-xs mb-2" style={{ color: S.text3 }}>
            {t("首页「批量分析」按钮一次启动多少个未分析过的 issue。范围")} {BATCH_TOP_N_BOUNDS.min}–{BATCH_TOP_N_BOUNDS.max}，{t("默认")} {BATCH_TOP_N_BOUNDS.default}。
          </p>
          <div className="flex items-center gap-2">
            <input
              type="number" min={BATCH_TOP_N_BOUNDS.min} max={BATCH_TOP_N_BOUNDS.max}
              value={draft} onChange={(e) => setDraft(e.target.value)}
              className="rounded px-3 py-1.5 text-sm w-24" style={inputStyle}
            />
            <button onClick={onSave} className="rounded px-3 py-1.5 text-sm font-medium"
              style={{ background: S.accent, color: "white", border: "none", cursor: "pointer" }}>
              {t("保存")}
            </button>
            {saved && <span className="text-xs" style={{ color: S.accent }}>✓ {t("已保存")}（{t("当前")}: {n}）</span>}
          </div>
          <p className="text-[11px] mt-2" style={{ color: S.text3 }}>{t("此偏好仅本浏览器有效（localStorage 存储）")}</p>
        </div>

        {/* 符号化设置 */}
        <div style={{ borderTop: `1px solid ${S.borderSm}`, paddingTop: 12 }}>
          <label className="block text-sm font-medium mb-3" style={{ color: S.text1 }}>{t("符号化缓存保留版本数")}</label>
          <div className="flex flex-col gap-3">
            <div>
              <label className="block text-xs mb-1" style={{ color: S.text2 }}>{t("上传符号包保留版本数（symbol_upload_keep_versions）")}</label>
              <p className="text-[11px] mb-1.5" style={{ color: S.text3 }}>{t("每次上传后，同平台+同类型最多保留最新 N 个版本，旧版本文件自动删除。范围 1–50。")}</p>
              <input
                type="number" min={1} max={50}
                value={symDraft.symbol_upload_keep_versions}
                onChange={(e) => setSymDraft((d) => ({ ...d, symbol_upload_keep_versions: e.target.value }))}
                className="rounded px-3 py-1.5 text-sm w-24" style={inputStyle}
              />
            </div>
            <div>
              <label className="block text-xs mb-1" style={{ color: S.text2 }}>{t("GitHub Release 缓存保留版本数（github_cache_keep_versions）")}</label>
              <p className="text-[11px] mb-1.5" style={{ color: S.text3 }}>{t("从 GitHub Release 自动下载的符号包，最多缓存 N 个版本（每版本约 200MB），按访问时间淘汰。范围 1–50。")}</p>
              <input
                type="number" min={1} max={50}
                value={symDraft.github_cache_keep_versions}
                onChange={(e) => setSymDraft((d) => ({ ...d, github_cache_keep_versions: e.target.value }))}
                className="rounded px-3 py-1.5 text-sm w-24" style={inputStyle}
              />
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={onSaveSymbols} disabled={symSaving}
                className="rounded px-3 py-1.5 text-sm font-medium"
                style={{ background: symSaving ? S.text3 : S.accent, color: "white", border: "none", cursor: symSaving ? "not-allowed" : "pointer" }}
              >
                {symSaving ? t("保存中...") : t("保存")}
              </button>
              {symSaved && <span className="text-xs" style={{ color: S.accent }}>✓ {t("已保存并持久化到 config.yaml")}</span>}
              {symError && <span className="text-xs" style={{ color: "#EF4444" }}>{symError}</span>}
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}


const PLATFORMS = ["android", "ios", "web", "desktop"] as const;
type Platform = typeof PLATFORMS[number];

const EMPTY_BAND: RepoBand = { min_version: "", family: "", wrapper: "", sub: "", github_repo: "", symbol_profile: "" };

function RepoRoutingSection() {
  const t = useT();

  const [routing, setRouting] = useState<Record<string, { bands: RepoBand[] }>>({});
  const [serviceFilter, setServiceFilter] = useState("");
  const [supportWeb, setSupportWeb] = useState(false);
  const [supportDesktop, setSupportDesktop] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [saveError, setSaveError] = useState("");

  // Preview widget state
  const [previewPlatform, setPreviewPlatform] = useState<Platform>("android");
  const [previewVersion, setPreviewVersion] = useState("");
  const [previewing, setPreviewing] = useState(false);
  const [previewResult, setPreviewResult] = useState<RepoRoutingPreviewResult | null>(null);

  useEffect(() => {
    getRepoRouting().then((data: RepoRoutingConfig) => {
      setRouting(data.routing || {});
      setServiceFilter(data.service_filter || "");
      setSupportWeb(!!data.support_web);
      setSupportDesktop(!!data.support_desktop);
    }).catch(console.error);
  }, []);

  const getBands = (platform: string): RepoBand[] =>
    routing[platform]?.bands || [];

  const setBands = (platform: string, bands: RepoBand[]) =>
    setRouting((prev) => ({ ...prev, [platform]: { ...prev[platform], bands } }));

  const updateBand = (platform: string, idx: number, field: keyof RepoBand, value: string) => {
    const bands = getBands(platform).map((b, i) => i === idx ? { ...b, [field]: value } : b);
    setBands(platform, bands);
  };

  const addBand = (platform: string) =>
    setBands(platform, [...getBands(platform), { ...EMPTY_BAND }]);

  const removeBand = (platform: string, idx: number) =>
    setBands(platform, getBands(platform).filter((_, i) => i !== idx));

  const onSave = async () => {
    setSaving(true);
    setSaveError("");
    try {
      await updateRepoRouting({ routing, service_filter: serviceFilter, support_web: supportWeb, support_desktop: supportDesktop });
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (e: any) {
      setSaveError(t("保存失败") + ": " + (e.message || ""));
    } finally {
      setSaving(false);
    }
  };

  const onPreview = async () => {
    setPreviewing(true);
    setPreviewResult(null);
    try {
      const res = await previewRepoRouting(previewPlatform, previewVersion || undefined);
      setPreviewResult(res);
    } catch (e: any) {
      setPreviewResult({ resolved: false, reason: e.message || t("预览失败") });
    } finally {
      setPreviewing(false);
    }
  };

  const bandHeaderCols = [
    t("最低版本"), t("代码族"), "Wrapper", t("子模块路径"), t("GitHub 仓库"), t("符号化配置"), ""
  ];

  return (
    <section className="rounded-xl p-5" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
      <div className="mb-4 flex items-center justify-between">
        <div>
          <h2 className="text-xs font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>
            {t("源码仓库路由")}
          </h2>
          <p className="mt-0.5 text-xs" style={{ color: S.text3 }}>
            {t("平台版本路由规则，用于 Crashguard 符号化与代码定位")}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {saved && <span className="text-xs" style={{ color: S.accent }}>✓ {t("路由配置已保存")}</span>}
          {saveError && <span className="text-xs" style={{ color: "#EF4444" }}>{saveError}</span>}
          <button
            onClick={onSave} disabled={saving}
            className="rounded-lg px-4 py-1.5 text-sm font-semibold disabled:opacity-50 transition-opacity"
            style={{ background: S.overlay, color: S.text1, border: `1px solid ${S.border}` }}
          >
            {saving ? t("保存中...") : t("保存路由配置")}
          </button>
        </div>
      </div>

      <div className="space-y-5">
        {/* Service Filter */}
        <div>
          <label className="mb-1.5 block text-xs font-medium" style={{ color: S.text2 }}>
            {t("Service 过滤器")}
          </label>
          <input
            type="text"
            value={serviceFilter}
            onChange={(e) => setServiceFilter(e.target.value)}
            placeholder="e.g. plaud-android-native"
            className="w-full max-w-sm rounded-lg px-3 py-2 text-sm font-mono"
            style={inputStyle}
          />
          <p className="mt-1 text-[11px]" style={{ color: S.text3 }}>
            {t("Datadog native service tag，上线前需实测确认")}
          </p>
        </div>

        {/* Platform support toggles — 控制 submit 页 web/desktop 是否可选 */}
        <div style={{ borderTop: `1px solid ${S.borderSm}`, paddingTop: 12 }}>
          <label className="mb-2 block text-xs font-medium" style={{ color: S.text2 }}>
            {t("支持的工单平台")}
          </label>
          <div className="flex flex-col gap-2">
            <label className="flex items-center gap-2 text-sm cursor-pointer" style={{ color: S.text1 }}>
              <input
                type="checkbox"
                checked={supportWeb}
                onChange={(e) => setSupportWeb(e.target.checked)}
                className="h-4 w-4 rounded"
                style={{ accentColor: S.accent }}
              />
              {t("支持 Web 工单")}
            </label>
            <label className="flex items-center gap-2 text-sm cursor-pointer" style={{ color: S.text1 }}>
              <input
                type="checkbox"
                checked={supportDesktop}
                onChange={(e) => setSupportDesktop(e.target.checked)}
                className="h-4 w-4 rounded"
                style={{ accentColor: S.accent }}
              />
              {t("支持 Desktop 工单")}
            </label>
          </div>
          <p className="mt-1.5 text-[11px]" style={{ color: S.text3 }}>
            {t("关闭后提交页无法选择该平台（默认关闭，为 4.0 native 做准备）")}
          </p>
        </div>

        {/* Per-platform bands */}
        {PLATFORMS.map((platform) => {
          const bands = getBands(platform);
          return (
            <div key={platform} style={{ borderTop: `1px solid ${S.borderSm}`, paddingTop: 12 }}>
              <div className="mb-2 flex items-center justify-between">
                <span className="text-xs font-semibold uppercase tracking-wide" style={{ color: S.text2 }}>
                  {platform}
                </span>
                <button
                  onClick={() => addBand(platform)}
                  className="rounded px-2 py-0.5 text-xs font-medium"
                  style={{ background: S.accentBg, color: S.accent, border: `1px solid rgba(14,124,134,0.2)` }}
                >
                  + {t("添加行")}
                </button>
              </div>
              {bands.length === 0 ? (
                <p className="text-xs py-1" style={{ color: S.text3 }}>{t("暂无配置")}</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-xs" style={{ borderCollapse: "collapse" }}>
                    <thead>
                      <tr>
                        {bandHeaderCols.map((col, i) => (
                          <th key={i} className="pb-1.5 pr-2 text-left font-semibold" style={{ color: S.text3 }}>
                            {col}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {bands.map((band, idx) => (
                        <tr key={idx} style={{ borderTop: `1px solid ${S.borderSm}` }}>
                          {(["min_version", "family", "wrapper", "sub", "github_repo", "symbol_profile"] as Array<keyof RepoBand>).map((field) => (
                            <td key={field} className="py-1 pr-1.5">
                              <input
                                type="text"
                                value={band[field]}
                                onChange={(e) => updateBand(platform, idx, field, e.target.value)}
                                className="rounded px-2 py-1 text-xs font-mono w-full"
                                style={{ ...inputStyle, minWidth: field === "github_repo" || field === "symbol_profile" ? 120 : 80 }}
                                placeholder={field}
                              />
                            </td>
                          ))}
                          <td className="py-1 pl-1">
                            <button
                              onClick={() => removeBand(platform, idx)}
                              className="rounded px-2 py-0.5 text-xs"
                              style={{ background: "rgba(239,68,68,0.08)", color: "#EF4444", border: "1px solid rgba(239,68,68,0.2)" }}
                            >
                              {t("删除")}
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          );
        })}

        {/* Resolution Preview */}
        <div style={{ borderTop: `1px solid ${S.borderSm}`, paddingTop: 12 }}>
          <h3 className="mb-3 text-xs font-semibold" style={{ color: S.text2 }}>{t("解析预览")}</h3>
          <div className="flex items-end gap-2 flex-wrap">
            <div>
              <label className="mb-1 block text-[11px]" style={{ color: S.text3 }}>{t("平台")}</label>
              <select
                value={previewPlatform}
                onChange={(e) => setPreviewPlatform(e.target.value as Platform)}
                className="rounded px-2 py-1.5 text-xs"
                style={inputStyle}
              >
                {PLATFORMS.map((p) => <option key={p} value={p}>{p}</option>)}
              </select>
            </div>
            <div>
              <label className="mb-1 block text-[11px]" style={{ color: S.text3 }}>{t("版本号")}</label>
              <input
                type="text"
                value={previewVersion}
                onChange={(e) => setPreviewVersion(e.target.value)}
                placeholder="e.g. 3.2.1"
                className="rounded px-2 py-1.5 text-xs font-mono w-32"
                style={inputStyle}
              />
            </div>
            <button
              onClick={onPreview} disabled={previewing}
              className="rounded px-3 py-1.5 text-xs font-medium disabled:opacity-50"
              style={{ background: S.accent, color: "white", border: "none" }}
            >
              {previewing ? t("预览中...") : t("预览")}
            </button>
          </div>

          {previewResult && (
            <div className="mt-3 rounded-lg p-3 text-xs" style={{
              background: previewResult.resolved ? "rgba(22,163,74,0.06)" : "rgba(239,68,68,0.06)",
              border: `1px solid ${previewResult.resolved ? "rgba(22,163,74,0.2)" : "rgba(239,68,68,0.2)"}`,
            }}>
              {previewResult.resolved ? (
                <>
                  <p className="font-semibold mb-1" style={{ color: "#16A34A" }}>✓ {t("命中")}</p>
                  <div className="grid grid-cols-2 gap-1">
                    {([
                      ["family", previewResult.family],
                      ["sub_repo_path", previewResult.sub_repo_path],
                      ["github_repo", previewResult.github_repo],
                      ["symbol_profile", previewResult.symbol_profile],
                      ["confidence", previewResult.confidence !== undefined ? String(previewResult.confidence) : undefined],
                    ] as [string, string | undefined][]).filter(([, v]) => v !== undefined && v !== "").map(([k, v]) => (
                      <div key={k}>
                        <span style={{ color: S.text3 }}>{k}: </span>
                        <span className="font-mono" style={{ color: S.text1 }}>{v}</span>
                      </div>
                    ))}
                  </div>
                </>
              ) : (
                <p style={{ color: "#EF4444" }}>✗ {t("未命中")}: {previewResult.reason || t("无法解析")}</p>
              )}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}


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
              onMouseEnter={(e) => (e.currentTarget.style.background = S.hover)}
              onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}>
              <td className="py-2.5 pr-4 font-medium" style={{ color: S.text1 }}>{u.username}</td>
              <td className="py-2.5 pr-4">
                <span className="rounded-full px-2 py-0.5 text-[10px] font-semibold"
                  style={u.role === "admin"
                    ? { background: S.accentBg, color: S.accent, border: "1px solid rgba(14,124,134,0.25)" }
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

  const [escalationMembers, setEscalationMembers] = useState("");
  const [escalationSaving, setEscalationSaving] = useState(false);

  const [condensation, setCondensation] = useState<CondensationConfig | null>(null);
  const [condensationSaving, setCondensationSaving] = useState(false);
  const [condensationApiKey, setCondensationApiKey] = useState("");

  const [autoDeepAnalysis, setAutoDeepAnalysis] = useState<AutoDeepAnalysisConfig | null>(null);
  const [autoDeepAnalysisSaving, setAutoDeepAnalysisSaving] = useState(false);

  const username = typeof window !== "undefined" ? localStorage.getItem("appllo_username") || "" : "";
  const isAdmin = username === "sanato";

  useEffect(() => {
    fetchAgentConfig().then(setConfig).catch(console.error);
    fetchHealth().then(setHealth).catch(console.error);
    checkAgents().then(setAgents).catch(console.error);
    fetchEscalationMembers()
      .then((data) => setEscalationMembers(data.members.join("\n")))
      .catch(console.error);
    fetchCondensationConfig().then(setCondensation).catch(console.error);
    fetchAutoDeepAnalysisConfig().then(setAutoDeepAnalysis).catch(console.error);
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
      await updateAgentConfig({ default_agent: config.default, call_mode: config.call_mode, api_traffic_ratio: config.api_traffic_ratio, timeout: config.timeout, max_turns: config.max_turns, routing: config.routing });
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

  const saveEscalationMembers = async () => {
    setEscalationSaving(true);
    try {
      const members = escalationMembers.split("\n").map((s) => s.trim()).filter(Boolean);
      await updateEscalationMembers(members);
      setToast(t("已保存"));
    } catch (e: any) { setToast(t("保存失败") + ": " + e.message); }
    finally { setEscalationSaving(false); }
  };

  const saveCondensation = async () => {
    if (!condensation) return;
    setCondensationSaving(true);
    try {
      await updateCondensationConfig({
        enabled: condensation.enabled,
        provider: condensation.provider,
        model: condensation.model,
        log_size_threshold_mb: condensation.log_size_threshold_mb,
        time_window_hours_before: condensation.time_window_hours_before,
        time_window_hours_after: condensation.time_window_hours_after,
        timeout: condensation.timeout,
        ...(condensationApiKey ? { api_key: condensationApiKey } : {}),
      });
      setToast(t("L1.5 配置已保存"));
      setCondensationApiKey("");
      fetchCondensationConfig().then(setCondensation).catch(console.error);
    } catch (e: any) { setToast(t("保存失败") + ": " + e.message); }
    finally { setCondensationSaving(false); }
  };

  const saveAutoDeepAnalysis = async () => {
    if (!autoDeepAnalysis) return;
    setAutoDeepAnalysisSaving(true);
    try {
      await updateAutoDeepAnalysisConfig({ enabled: autoDeepAnalysis.enabled });
      setToast(t("已保存"));
    } catch (e: any) { setToast(t("保存失败") + ": " + e.message); }
    finally { setAutoDeepAnalysisSaving(false); }
  };

  const ruleTypes = ["recording_missing", "timestamp_drift", "bluetooth", "cloud_sync", "speaker", "flutter_crash", "file_transfer", "membership_payment", "hardware_firmware", "general"];

  const statusColor = (s: string) =>
    s === "ok" || s === "healthy" ? "#16A34A" :
    s === "unavailable" ? "#CA8A04" : "#DC2626";

  return (
    <div className="min-h-full">
      {/* Header */}
      <header className="sticky top-0 z-10 backdrop-blur-md j-rise"
        style={{ background: "var(--j-header)", borderBottom: `1px solid ${S.border}` }}>
        <div className="flex items-center justify-between px-6 py-3">
          <div>
            <h1 className="text-base font-semibold" style={{ color: S.text1 }}>{t("系统设置")}</h1>
            <p className="text-xs mt-0.5" style={{ color: S.text3 }}>{t("Agent 配置、环境变量与系统状态")}</p>
          </div>
          <button onClick={saveAgentConfig} disabled={saving}
            className="rounded-lg px-4 py-1.5 text-sm font-semibold disabled:opacity-50 transition-opacity"
            style={{ background: S.accent, color: "#FFFFFF" }}>
            {saving ? t("保存中...") : t("保存 Agent 配置")}
          </button>
        </div>
      </header>

      <div className="mx-auto max-w-3xl px-6 py-6 space-y-5 j-rise" style={{ ["--d" as string]: "0.06s" }}>

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
            style={{ background: S.accentBg, border: "1px solid rgba(14,124,134,0.2)", color: S.text2 }}>
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

        {/* CLAUDE CALL MODE TOGGLE */}
        {config && (
          <section className="rounded-xl p-5" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
            <h2 className="mb-4 text-xs font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>{t("Claude 调用方式")}</h2>
            <p className="text-xs mb-4" style={{ color: S.text3 }}>
              {t("调整后下一个分析任务立即生效；L1.5 浓缩永远走 API（与此开关无关）。")}
            </p>
            {/* API traffic ratio slider */}
            <div className="rounded-lg p-4" style={{ background: S.overlay, border: `1px solid ${S.border}` }}>
              <div className="flex items-center justify-between mb-3">
                <span className="text-sm font-medium" style={{ color: S.text1 }}>{t("API 流量比例")}</span>
                <div className="flex items-center gap-2">
                  <span className="text-xs px-2 py-0.5 rounded font-mono"
                    style={{ background: S.accentBg, color: S.accent, border: `1px solid color-mix(in srgb, ${S.accent} 30%, transparent)` }}>
                    API {Math.round((config.api_traffic_ratio ?? 0) * 100)}%
                  </span>
                  <span className="text-xs" style={{ color: S.text3 }}>
                    CLI {100 - Math.round((config.api_traffic_ratio ?? 0) * 100)}%
                  </span>
                </div>
              </div>
              <input
                type="range"
                min={0} max={100} step={5}
                value={Math.round((config.api_traffic_ratio ?? 0) * 100)}
                onChange={(e) => setConfig({ ...config, api_traffic_ratio: Number(e.target.value) / 100 })}
                style={{ width: "100%", accentColor: S.accent }}
              />
              <div className="flex justify-between mt-1">
                <span className="text-[10px]" style={{ color: S.text3 }}>0% (全 CLI)</span>
                <span className="text-[10px]" style={{ color: S.text3 }}>50%</span>
                <span className="text-[10px]" style={{ color: S.text3 }}>100% (全 API)</span>
              </div>
              <p className="text-xs mt-3" style={{ color: S.text3 }}>
                {t("API 直连")}：{t("通过公司 Vertex 代理调用；可观测每一步、无 CLI 依赖。")}<br />
                {t("CLI 子进程")}：{t("保留原 claude CLI 调用方式，行为与历史一致。")}
              </p>
            </div>
          </section>
        )}

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

          </>
        )}

        {/* ESCALATION FIXED MEMBERS */}
        <section className="rounded-xl p-5" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
          <div className="mb-4 flex items-center justify-between">
            <div>
              <h2 className="text-xs font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>
                {t("转交群固定成员")}
              </h2>
              <p className="mt-0.5 text-xs" style={{ color: S.text3 }}>
                {t("每次创建转交群时，这些人会被自动邀请")}
              </p>
            </div>
            <button onClick={saveEscalationMembers} disabled={escalationSaving}
              className="rounded-lg px-4 py-1.5 text-sm font-semibold disabled:opacity-50 transition-opacity"
              style={{ background: S.overlay, color: S.text1, border: `1px solid ${S.border}` }}>
              {escalationSaving ? t("保存中...") : t("保存成员列表")}
            </button>
          </div>
          <textarea
            value={escalationMembers}
            onChange={(e) => setEscalationMembers(e.target.value)}
            placeholder={t("每行一个邮箱地址")}
            rows={8}
            className="w-full resize-none rounded-lg px-3 py-2 font-mono text-sm transition-colors"
            style={inputStyle}
          />
        </section>

        {/* AUTO DEEP ANALYSIS */}
        {autoDeepAnalysis && (
          <section className="rounded-xl p-5" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
            <div className="mb-4 flex items-center justify-between">
              <div>
                <h2 className="text-xs font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>
                  {t("自动升级 Deep Analysis")}
                </h2>
                <p className="mt-0.5 text-xs" style={{ color: S.text3 }}>
                  {t("分析置信度为 low 时自动重跑深度分析，完成后飞书通知创建人")}
                </p>
              </div>
              <button onClick={saveAutoDeepAnalysis} disabled={autoDeepAnalysisSaving}
                className="rounded-lg px-4 py-1.5 text-sm font-semibold disabled:opacity-50 transition-opacity"
                style={{ background: S.overlay, color: S.text1, border: `1px solid ${S.border}` }}>
                {autoDeepAnalysisSaving ? t("保存中...") : t("保存配置")}
              </button>
            </div>
            <label className="flex cursor-pointer items-center gap-3">
              <input type="checkbox" checked={autoDeepAnalysis.enabled}
                onChange={(e) => setAutoDeepAnalysis({ ...autoDeepAnalysis, enabled: e.target.checked })}
                className="h-4 w-4 rounded" style={{ accentColor: S.accent }} />
              <span className="text-sm font-medium" style={{ color: S.text1 }}>{t("自动升级 Deep Analysis")}</span>
              {autoDeepAnalysis.enabled && (
                <span className="rounded-full px-2 py-0.5 text-[10px] font-semibold"
                  style={{ background: "rgba(22,163,74,0.1)", color: "#16A34A" }}>{t("已启用")}</span>
              )}
            </label>
          </section>
        )}

        {/* L1.5 CONTEXT CONDENSATION */}
        {condensation && (
          <section className="rounded-xl p-5" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
            <div className="mb-4 flex items-center justify-between">
              <div>
                <h2 className="text-xs font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>
                  {t("L1.5 日志预提取")}
                </h2>
                <p className="mt-0.5 text-xs" style={{ color: S.text3 }}>
                  {t("用大 context 模型预提取日志关键信息，减少分析超时")}
                </p>
              </div>
              <button onClick={saveCondensation} disabled={condensationSaving}
                className="rounded-lg px-4 py-1.5 text-sm font-semibold disabled:opacity-50 transition-opacity"
                style={{ background: S.overlay, color: S.text1, border: `1px solid ${S.border}` }}>
                {condensationSaving ? t("保存中...") : t("保存配置")}
              </button>
            </div>

            <div className="space-y-4">
              {/* Enable toggle */}
              <label className="flex cursor-pointer items-center gap-3">
                <input type="checkbox" checked={condensation.enabled}
                  onChange={(e) => setCondensation({ ...condensation, enabled: e.target.checked })}
                  className="h-4 w-4 rounded" style={{ accentColor: S.accent }} />
                <span className="text-sm font-medium" style={{ color: S.text1 }}>{t("启用 L1.5 预提取")}</span>
                {condensation.enabled && (
                  <span className="rounded-full px-2 py-0.5 text-[10px] font-semibold"
                    style={{ background: "rgba(22,163,74,0.1)", color: "#16A34A" }}>{t("已启用")}</span>
                )}
              </label>

              <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                {/* Provider */}
                <div>
                  <label className="mb-1.5 block text-xs font-medium" style={{ color: S.text2 }}>{t("模型提供商")}</label>
                  <select value={condensation.provider}
                    onChange={(e) => {
                      const p = e.target.value;
                      setCondensation({ ...condensation, provider: p, model: "" });
                    }}
                    className="w-full rounded-lg px-3 py-2 text-sm" style={inputStyle}>
                    <option value="anthropic">Anthropic (Claude Haiku)</option>
                    <option value="gemini">Google (Gemini Flash)</option>
                    <option value="openai">OpenAI (GPT-4.1 mini)</option>
                  </select>
                  <p className="mt-1 text-[10px]" style={{ color: S.text3 }}>
                    {t("默认模型")}: {condensation.default_models?.[condensation.provider] || "?"}
                  </p>
                </div>

                {/* Model override */}
                <div>
                  <label className="mb-1.5 block text-xs font-medium" style={{ color: S.text2 }}>{t("模型（留空用默认）")}</label>
                  <input type="text" value={condensation.model}
                    onChange={(e) => setCondensation({ ...condensation, model: e.target.value })}
                    placeholder={condensation.default_models?.[condensation.provider] || ""}
                    className="w-full rounded-lg px-3 py-2 text-sm" style={inputStyle} />
                </div>

                {/* API Key */}
                <div>
                  <label className="mb-1.5 block text-xs font-medium" style={{ color: S.text2 }}>API Key</label>
                  <input type="password" value={condensationApiKey}
                    onChange={(e) => setCondensationApiKey(e.target.value)}
                    placeholder={condensation.has_api_key ? condensation.api_key_masked : t("输入 API Key")}
                    className="w-full rounded-lg px-3 py-2 font-mono text-sm" style={inputStyle} />
                  {condensation.has_api_key && !condensationApiKey && (
                    <p className="mt-1 text-[10px]" style={{ color: "#16A34A" }}>✓ {t("已配置")}</p>
                  )}
                </div>

                {/* Timeout */}
                <div>
                  <label className="mb-1.5 block text-xs font-medium" style={{ color: S.text2 }}>{t("超时（秒）")}</label>
                  <input type="number" value={condensation.timeout}
                    onChange={(e) => setCondensation({ ...condensation, timeout: parseInt(e.target.value) || 120 })}
                    className="w-full rounded-lg px-3 py-2 text-sm" style={inputStyle} />
                </div>

                {/* Log size threshold */}
                <div>
                  <label className="mb-1.5 block text-xs font-medium" style={{ color: S.text2 }}>{t("日志阈值 (MB)")}</label>
                  <input type="number" value={condensation.log_size_threshold_mb} step="1"
                    onChange={(e) => setCondensation({ ...condensation, log_size_threshold_mb: parseFloat(e.target.value) || 5 })}
                    className="w-full rounded-lg px-3 py-2 text-sm" style={inputStyle} />
                  <p className="mt-1 text-[10px]" style={{ color: S.text3 }}>
                    {t("仅对大于此值的日志启用预提取")}
                  </p>
                </div>

                {/* Time window */}
                <div>
                  <label className="mb-1.5 block text-xs font-medium" style={{ color: S.text2 }}>{t("时间窗口")}</label>
                  <div className="flex items-center gap-2">
                    <span className="text-xs" style={{ color: S.text3 }}>{t("前")}</span>
                    <input type="number" value={condensation.time_window_hours_before} min={1} max={24}
                      onChange={(e) => setCondensation({ ...condensation, time_window_hours_before: parseInt(e.target.value) || 4 })}
                      className="w-16 rounded-lg px-2 py-2 text-center text-sm" style={inputStyle} />
                    <span className="text-xs" style={{ color: S.text3 }}>{t("小时 / 后")}</span>
                    <input type="number" value={condensation.time_window_hours_after} min={1} max={24}
                      onChange={(e) => setCondensation({ ...condensation, time_window_hours_after: parseInt(e.target.value) || 2 })}
                      className="w-16 rounded-lg px-2 py-2 text-center text-sm" style={inputStyle} />
                    <span className="text-xs" style={{ color: S.text3 }}>{t("小时")}</span>
                  </div>
                </div>
              </div>

              {/* Info box */}
              <div className="rounded-lg p-3 text-xs" style={{ background: S.accentBg, border: `1px solid rgba(14,124,134,0.15)` }}>
                <p style={{ color: S.accent }}><strong>L1.5 {t("工作原理")}:</strong></p>
                <p className="mt-1" style={{ color: S.text2 }}>
                  {t("① 时间窗口切割（自动，免费）：大日志按问题日期裁剪，通常减少 80-95% 体积")}
                </p>
                <p className="mt-0.5" style={{ color: S.text2 }}>
                  {t("② LLM 上下文提取（需 API Key）：用便宜模型阅读日志，提取结构化关键信息给分析 Agent")}
                </p>
                <p className="mt-0.5" style={{ color: S.text2 }}>
                  {t("即使不启用 LLM 提取，时间窗口切割也会自动生效，已能显著减少超时")}
                </p>
              </div>
            </div>
          </section>
        )}

        {/* REPO ROUTING */}
        <RepoRoutingSection />

        {/* CRASHGUARD PREFERENCES (per-browser) */}
        <CrashguardPrefsSection />

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
