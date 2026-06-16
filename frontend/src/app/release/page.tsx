"use client";

import { useEffect, useMemo, useState } from "react";
import { Toast } from "@/components/Toast";
import {
  createReleaseBranch,
  listReleaseBranches,
  listReleaseSourceBranches,
  triggerReleaseBuild,
  listReleaseBuilds,
  releaseArtifactUrl,
  type ReleaseBranch,
  type ReleaseBuild,
} from "@/lib/api";

const S = {
  surface: "#F1F4F3",
  overlay: "#FFFFFF",
  hover: "#E8ECEA",
  border: "rgba(0,0,0,0.08)",
  accent: "#0E7C86",
  accentBg: "rgba(14,124,134,0.06)",
  text1: "#15181E",
  text2: "#5B6470",
  text3: "#9CA3AF",
};

const inputStyle = {
  background: S.overlay,
  border: `1px solid ${S.border}`,
  color: S.text1,
  outline: "none",
};

const BRANCH_RE = /^release\/(\d+)\.(\d+)\.(\d+)_(\d{4})$/;

function fmtTime(iso: string | null) {
  if (!iso) return "—";
  const d = new Date(iso);
  return `${(d.getMonth() + 1).toString().padStart(2, "0")}-${d.getDate().toString().padStart(2, "0")} ${d.getHours().toString().padStart(2, "0")}:${d.getMinutes().toString().padStart(2, "0")}`;
}

function durationLabel(start: string | null, end: string | null) {
  if (!start) return "—";
  const s = new Date(start).getTime();
  const e = end ? new Date(end).getTime() : Date.now();
  const sec = Math.max(0, Math.round((e - s) / 1000));
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m${sec % 60}s`;
  return `${Math.floor(sec / 3600)}h${Math.floor((sec % 3600) / 60)}m`;
}

const statusColor: Record<ReleaseBuild["status"], { bg: string; fg: string; label: string }> = {
  pending: { bg: "#F1F5F9", fg: "#475569", label: "待触发" },
  queued: { bg: "#E0E7FF", fg: "#3730A3", label: "排队中" },
  running: { bg: "#DBEAFE", fg: "#1D4ED8", label: "构建中" },
  success: { bg: "#DCFCE7", fg: "#15803D", label: "成功" },
  failure: { bg: "#FEE2E2", fg: "#B91C1C", label: "失败" },
  aborted: { bg: "#FFEDD5", fg: "#9A3412", label: "已中止" },
  error: { bg: "#FECACA", fg: "#7F1D1D", label: "异常" },
};

function defaultBranchTemplate() {
  const today = new Date();
  const mmdd = `${(today.getMonth() + 1).toString().padStart(2, "0")}${today.getDate().toString().padStart(2, "0")}`;
  return `release/X.Y.Z_${mmdd}`;
}

export default function ReleasePage() {
  // ─── create-branch state ──────────────────────────────────────────────
  const [branchInput, setBranchInput] = useState("release/");
  const [sourceBranch, setSourceBranch] = useState("main");
  const [sourceOptions, setSourceOptions] = useState<string[]>(["main"]);
  const [loadingSources, setLoadingSources] = useState(false);
  const [creating, setCreating] = useState(false);
  const [recentBranch, setRecentBranch] = useState<ReleaseBranch | null>(null);

  // ─── build state ──────────────────────────────────────────────────────
  const [branches, setBranches] = useState<ReleaseBranch[]>([]);
  const [buildBranch, setBuildBranch] = useState("");
  const [buildTarget, setBuildTarget] = useState<"global" | "cn">("global");
  const [androidMultiChannel, setAndroidMultiChannel] = useState(true);   // cn default true
  const [isOnlinePackage, setIsOnlinePackage] = useState(true);
  const [uploadToGithub, setUploadToGithub] = useState(true);
  const [skipAscUpload, setSkipAscUpload] = useState(false);
  const [buildDescription, setBuildDescription] = useState("");
  const [triggering, setTriggering] = useState(false);

  // ─── history ──────────────────────────────────────────────────────────
  const [builds, setBuilds] = useState<ReleaseBuild[]>([]);
  const [loadingBuilds, setLoadingBuilds] = useState(false);

  const [toast, setToast] = useState<{ msg: string; type: "success" | "error" } | null>(null);

  const branchValid = useMemo(() => BRANCH_RE.test(branchInput), [branchInput]);
  // Source must match a real branch from the server-fetched list — never
  // trust free-form input. Empty list while loading shows a benign warning.
  const sourceValid = useMemo(
    () => sourceOptions.includes(sourceBranch),
    [sourceOptions, sourceBranch],
  );

  // ─── data loaders ─────────────────────────────────────────────────────
  const loadBranches = async () => {
    try {
      const r = await listReleaseBranches(50, 0);
      setBranches(r.items);
      if (!buildBranch && r.items.length > 0) {
        setBuildBranch(r.items[0].branch);
      }
    } catch (e: any) {
      console.error(e);
    }
  };

  const loadBuilds = async () => {
    setLoadingBuilds(true);
    try {
      const r = await listReleaseBuilds(undefined, 50, 0);
      setBuilds(r.items);
    } catch (e: any) {
      console.error(e);
    } finally {
      setLoadingBuilds(false);
    }
  };

  const loadSourceBranches = async () => {
    setLoadingSources(true);
    try {
      const r = await listReleaseSourceBranches();
      // Always keep `main` first even if backend is briefly empty.
      const list = r.branches && r.branches.length > 0 ? r.branches : ["main"];
      setSourceOptions(list);
      // If current selection isn't in the new list, reset to main.
      if (!list.includes(sourceBranch)) {
        setSourceBranch("main");
      }
    } catch (e: any) {
      console.error(e);
      // Keep the dropdown usable with `main` fallback so the default flow
      // still works when the workspace is briefly busy.
      setSourceOptions(["main"]);
    } finally {
      setLoadingSources(false);
    }
  };

  useEffect(() => {
    loadBranches();
    loadBuilds();
    loadSourceBranches();
    const t = setInterval(loadBuilds, 10_000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ─── handlers ─────────────────────────────────────────────────────────
  const handleCreateBranch = async () => {
    if (!branchValid) {
      setToast({ msg: "分支名格式不正确，应为 release/X.Y.Z_MMDD", type: "error" });
      return;
    }
    if (!sourceValid) {
      setToast({
        msg: `source 分支 "${sourceBranch}" 不在候选项中，必须从下拉列表中选择真实存在的分支`,
        type: "error",
      });
      return;
    }
    setCreating(true);
    try {
      const b = await createReleaseBranch(branchInput, sourceBranch);
      setRecentBranch(b);
      setToast({ msg: `分支已创建：${b.branch}（来源：${sourceBranch}）`, type: "success" });
      setBranchInput("release/");
      setSourceBranch("main");
      await loadBranches();
      await loadSourceBranches();
      setBuildBranch(b.branch);
    } catch (e: any) {
      setToast({ msg: e?.message || "创建失败", type: "error" });
    } finally {
      setCreating(false);
    }
  };

  const handleTriggerBuild = async () => {
    if (!buildBranch) {
      setToast({ msg: "请先选择 release 分支", type: "error" });
      return;
    }
    setTriggering(true);
    try {
      const b = await triggerReleaseBuild(buildBranch, buildTarget, {
        is_online_package: isOnlinePackage,
        upload_to_github_release: uploadToGithub,
        skip_asc_upload: buildTarget === "global" ? skipAscUpload : undefined,
        android_multi_channel_pack: buildTarget === "cn" ? androidMultiChannel : undefined,
        description: buildDescription || undefined,
      });
      setToast({ msg: `构建已触发：build #${b.id}（Jenkins queue ${b.jenkins_queue_id}）`, type: "success" });
      await loadBuilds();
    } catch (e: any) {
      setToast({ msg: e?.message || "触发失败", type: "error" });
    } finally {
      setTriggering(false);
    }
  };

  // ─── render ───────────────────────────────────────────────────────────
  return (
    <div className="min-h-screen p-6" style={{ background: S.surface, color: S.text1 }}>
      {toast && (
        <Toast msg={toast.msg} type={toast.type} onClose={() => setToast(null)} />
      )}

      <div className="mx-auto max-w-6xl space-y-6">
        <div>
          <h1 className="text-xl font-semibold" style={{ color: S.text1 }}>
            Release 自动化
          </h1>
          <p className="mt-1 text-sm" style={{ color: S.text2 }}>
            创建 release 分支（多仓 fanout）和触发 Jenkins 构建两个独立操作。
          </p>
        </div>

        {/* ─── 1. 创建 release 分支 ────────────────────────────────── */}
        <section
          className="rounded-lg p-5"
          style={{ background: S.overlay, border: `1px solid ${S.border}` }}
        >
          <h2 className="text-base font-semibold" style={{ color: S.text1 }}>
            ① 创建 release 分支
          </h2>
          <p className="mt-1 text-xs" style={{ color: S.text2 }}>
            从 main 切出，多仓（common / global / cn）同步创建并 push。
          </p>
          <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-12 sm:items-end">
            <div className="sm:col-span-5">
              <label className="block text-xs" style={{ color: S.text2 }}>
                新分支名（格式 release/X.Y.Z_MMDD）
              </label>
              <input
                type="text"
                value={branchInput}
                onChange={(e) => setBranchInput(e.target.value.trim())}
                placeholder={defaultBranchTemplate()}
                className="mt-1 w-full rounded px-3 py-2 text-sm"
                style={{
                  ...inputStyle,
                  borderColor: branchInput && !branchValid ? "#DC2626" : S.border,
                }}
              />
              {branchInput && !branchValid && (
                <div className="mt-1 text-xs" style={{ color: "#DC2626" }}>
                  格式错误：期望 release/3.2.0_1222 形态
                </div>
              )}
            </div>
            <div className="sm:col-span-5">
              <label className="block text-xs" style={{ color: S.text2 }}>
                来源分支（默认 main；hotfix 可选 release/*）
                <span className="ml-1" style={{ color: S.text3 }}>
                  {loadingSources ? "（加载中…）" : `（${sourceOptions.length} 个候选）`}
                </span>
              </label>
              <input
                type="text"
                value={sourceBranch}
                onChange={(e) => setSourceBranch(e.target.value.trim())}
                list="release-source-options"
                placeholder="main"
                className="mt-1 w-full rounded px-3 py-2 text-sm"
                style={{
                  ...inputStyle,
                  borderColor: sourceBranch && !sourceValid ? "#DC2626" : S.border,
                }}
                autoComplete="off"
              />
              <datalist id="release-source-options">
                {sourceOptions.map((b) => (
                  <option key={b} value={b} />
                ))}
              </datalist>
              {sourceBranch && !sourceValid && (
                <div className="mt-1 text-xs" style={{ color: "#DC2626" }}>
                  必须从候选项中选择真实存在的分支
                </div>
              )}
            </div>
            <div className="sm:col-span-2">
              <button
                onClick={handleCreateBranch}
                disabled={creating || !branchValid || !sourceValid}
                className="w-full rounded px-4 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:opacity-50"
                style={{ background: S.accent }}
              >
                {creating ? "创建中…" : "创建分支"}
              </button>
            </div>
          </div>

          {recentBranch && (
            <div
              className="mt-4 rounded p-3 text-xs"
              style={{ background: S.accentBg, border: `1px solid ${S.border}` }}
            >
              <div className="font-medium" style={{ color: S.text1 }}>
                {recentBranch.branch}
              </div>
              <div className="mt-1 grid grid-cols-1 gap-1 sm:grid-cols-3">
                {recentBranch.repos.map((r) => (
                  <div key={r.name} style={{ color: S.text2 }}>
                    <span className="font-medium">{r.name}</span>:{" "}
                    <span className="font-mono">{r.commit_sha.slice(0, 8)}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </section>

        {/* ─── 2. 触发构建 ─────────────────────────────────────────── */}
        <section
          className="rounded-lg p-5"
          style={{ background: S.overlay, border: `1px solid ${S.border}` }}
        >
          <h2 className="text-base font-semibold" style={{ color: S.text1 }}>
            ② 触发构建
          </h2>
          <p className="mt-1 text-xs" style={{ color: S.text2 }}>
            在最空闲的 Jenkins 上启动构建（version bump 由 Jenkins pipeline 自己处理）。
          </p>
          <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-4">
            <div className="sm:col-span-2">
              <label className="block text-xs" style={{ color: S.text2 }}>
                release 分支
                <span className="ml-1" style={{ color: S.text3 }}>
                  （可从候选项选，也可手动输入）
                </span>
              </label>
              <input
                type="text"
                value={buildBranch}
                onChange={(e) => setBuildBranch(e.target.value.trim())}
                list="release-branch-options"
                placeholder="release/3.18.0_0520"
                className="mt-1 w-full rounded px-3 py-2 text-sm"
                style={inputStyle}
                autoComplete="off"
              />
              <datalist id="release-branch-options">
                {branches.map((b) => (
                  <option key={b.id} value={b.branch} />
                ))}
              </datalist>
            </div>

            <div>
              <label className="block text-xs" style={{ color: S.text2 }}>
                目标
              </label>
              <select
                value={buildTarget}
                onChange={(e) => setBuildTarget(e.target.value as "cn" | "global")}
                className="mt-1 w-full rounded px-3 py-2 text-sm"
                style={inputStyle}
              >
                <option value="global">global</option>
                <option value="cn">cn</option>
              </select>
            </div>

            <div className="flex items-end">
              <button
                onClick={handleTriggerBuild}
                disabled={triggering || !buildBranch}
                className="w-full rounded px-4 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:opacity-50"
                style={{ background: S.accent }}
              >
                {triggering ? "触发中…" : "触发构建"}
              </button>
            </div>
          </div>

          <div className="mt-4 grid grid-cols-1 gap-2 sm:grid-cols-2">
            <label className="flex items-center gap-2 text-xs" style={{ color: S.text2 }}>
              <input
                type="checkbox"
                checked={isOnlinePackage}
                onChange={(e) => setIsOnlinePackage(e.target.checked)}
              />
              IS_ONLINE_PACKAGE
            </label>
            <label className="flex items-center gap-2 text-xs" style={{ color: S.text2 }}>
              <input
                type="checkbox"
                checked={uploadToGithub}
                onChange={(e) => setUploadToGithub(e.target.checked)}
              />
              UPLOAD_TO_GITHUB_RELEASE
            </label>
            {buildTarget === "global" && (
              <label className="flex items-center gap-2 text-xs" style={{ color: S.text2 }}>
                <input
                  type="checkbox"
                  checked={skipAscUpload}
                  onChange={(e) => setSkipAscUpload(e.target.checked)}
                />
                SKIP_ASC_UPLOAD（跳过 AppStore Connect 上传）
              </label>
            )}
            {buildTarget === "cn" && (
              <label className="flex items-center gap-2 text-xs" style={{ color: S.text2 }}>
                <input
                  type="checkbox"
                  checked={androidMultiChannel}
                  onChange={(e) => setAndroidMultiChannel(e.target.checked)}
                />
                android_multi_channel_pack（多渠道打包）
              </label>
            )}
          </div>
          <div className="mt-3">
            <label className="block text-xs" style={{ color: S.text2 }}>
              description（可选）
            </label>
            <input
              type="text"
              value={buildDescription}
              onChange={(e) => setBuildDescription(e.target.value)}
              placeholder="本次构建的备注，留空即可"
              className="mt-1 w-full rounded px-3 py-2 text-sm"
              style={inputStyle}
            />
          </div>
        </section>

        {/* ─── 3. 构建历史 ─────────────────────────────────────────── */}
        <section
          className="rounded-lg p-5"
          style={{ background: S.overlay, border: `1px solid ${S.border}` }}
        >
          <div className="flex items-center justify-between">
            <h2 className="text-base font-semibold" style={{ color: S.text1 }}>
              ③ 构建历史
            </h2>
            <button
              onClick={loadBuilds}
              className="text-xs"
              style={{ color: S.text2 }}
            >
              {loadingBuilds ? "刷新中…" : "立即刷新"}
            </button>
          </div>
          <div className="mt-3 overflow-x-auto">
            <table className="min-w-full text-xs">
              <thead>
                <tr style={{ color: S.text2 }}>
                  <th className="px-2 py-2 text-left">ID</th>
                  <th className="px-2 py-2 text-left">分支</th>
                  <th className="px-2 py-2 text-left">目标</th>
                  <th className="px-2 py-2 text-left">状态</th>
                  <th className="px-2 py-2 text-left">Jenkins</th>
                  <th className="px-2 py-2 text-left">触发人</th>
                  <th className="px-2 py-2 text-left">耗时</th>
                  <th className="px-2 py-2 text-left">触发时间</th>
                  <th className="px-2 py-2 text-left">产物</th>
                </tr>
              </thead>
              <tbody>
                {builds.length === 0 && (
                  <tr>
                    <td colSpan={9} className="px-2 py-6 text-center" style={{ color: S.text3 }}>
                      暂无构建记录
                    </td>
                  </tr>
                )}
                {builds.map((b) => {
                  const sc = statusColor[b.status] || statusColor.pending;
                  return (
                    <tr key={b.id} style={{ borderTop: `1px solid ${S.border}` }}>
                      <td className="px-2 py-2 font-mono" style={{ color: S.text2 }}>
                        #{b.id}
                      </td>
                      <td className="px-2 py-2 font-mono">{b.branch}</td>
                      <td className="px-2 py-2">
                        {b.target}
                        {b.android_multi_channel && <span className="ml-1 text-xs" style={{ color: S.text3 }}>+mc</span>}
                      </td>
                      <td className="px-2 py-2">
                        <span
                          className="rounded px-2 py-0.5 text-xs"
                          style={{ background: sc.bg, color: sc.fg }}
                          title={b.error_message || ""}
                        >
                          {sc.label}
                        </span>
                      </td>
                      <td className="px-2 py-2" style={{ color: S.text2 }}>
                        {b.jenkins_build_url ? (
                          <a
                            href={b.jenkins_build_url}
                            target="_blank"
                            rel="noreferrer"
                            style={{ color: S.accent }}
                          >
                            #{b.jenkins_build_number ?? "?"}
                          </a>
                        ) : (
                          <span style={{ color: S.text3 }}>queue {b.jenkins_queue_id ?? "?"}</span>
                        )}
                        <div className="text-[10px]" style={{ color: S.text3 }}>
                          {b.jenkins_server.replace(/^https?:\/\//, "")}
                        </div>
                      </td>
                      <td className="px-2 py-2" style={{ color: S.text2 }}>
                        {b.triggered_by}
                      </td>
                      <td className="px-2 py-2" style={{ color: S.text2 }}>
                        {durationLabel(b.started_at, b.finished_at)}
                      </td>
                      <td className="px-2 py-2" style={{ color: S.text2 }}>
                        {fmtTime(b.triggered_at)}
                      </td>
                      <td className="px-2 py-2">
                        {b.status === "success" ? (
                          <div className="flex gap-2">
                            {b.artifact_android_url && (
                              <a
                                href={releaseArtifactUrl(b.id, "android")}
                                target="_blank"
                                rel="noreferrer"
                                style={{ color: S.accent }}
                              >
                                Android
                              </a>
                            )}
                            {b.artifact_ios_url && (
                              <a
                                href={releaseArtifactUrl(b.id, "ios")}
                                target="_blank"
                                rel="noreferrer"
                                style={{ color: S.accent }}
                              >
                                iOS
                              </a>
                            )}
                            {!b.artifact_android_url && !b.artifact_ios_url && (
                              <span style={{ color: S.text3 }}>无产物</span>
                            )}
                          </div>
                        ) : (
                          <span style={{ color: S.text3 }}>—</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>
      </div>
    </div>
  );
}
