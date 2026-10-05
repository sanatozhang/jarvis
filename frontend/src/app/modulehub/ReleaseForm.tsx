"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useT } from "@/lib/i18n";
import {
  getMhRelease,
  mhErrorDetail,
  previewMhRelease,
  startMhRelease,
  MH_ACTIVE_STATES,
  type MhModule,
  type MhPlatform,
  type MhRelease,
  type MhReleaseRequest,
} from "@/lib/api";
import { S, inputStyle, PLATFORMS, PLATFORM_LABEL, STATE_STYLE, isReleaseBranch, isValidBranch, sinceSeconds } from "./ui";

const PREVIEW_POLL_MS = 5000;
// A preview that is still building after this long usually means the poller is off (modulehub.enabled=false).
const STALL_HINT_SECONDS = 180;

function sameRequest(a: MhReleaseRequest, b: MhRelease): boolean {
  return a.module === b.module && a.branch === b.branch && a.major === b.major &&
    [...a.platforms].sort().join(",") === [...b.platforms].sort().join(",");
}

export function ReleaseForm({
  modules,
  module,
  onModuleChange,
  onStarted,
  notify,
}: {
  modules: MhModule[];
  module: string;
  onModuleChange: (m: string) => void;
  onStarted: (r: MhRelease) => void;
  notify: (msg: string, type: "success" | "error") => void;
}) {
  const t = useT();
  const [branch, setBranch] = useState("main");
  const [platforms, setPlatforms] = useState<MhPlatform[]>(["android", "ios"]);
  const [major, setMajor] = useState(false);
  const [preview, setPreview] = useState<MhRelease | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [, setTick] = useState(0);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const req: MhReleaseRequest = { module, branch, platforms, major };
  const branchOk = isValidBranch(branch);
  const majorBlocked = major && isReleaseBranch(branch);
  const valid = !!module && branchOk && platforms.length > 0 && !majorBlocked;
  const current = useMemo(() => modules.find((m) => m.name === module), [modules, module]);
  const previewActive = !!preview && MH_ACTIVE_STATES.includes(preview.state);
  const previewMatches = !!preview && sameRequest(req, preview);

  const stopPolling = () => {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = null;
  };
  useEffect(() => stopPolling, []);

  const pollPreview = (id: number) => {
    stopPolling();
    pollRef.current = setInterval(async () => {
      setTick((n) => n + 1);
      try {
        const r = await getMhRelease(id);
        setPreview(r);
        if (!MH_ACTIVE_STATES.includes(r.state)) stopPolling();
      } catch (e) {
        stopPolling();
        notify(mhErrorDetail(e), "error");
      }
    }, PREVIEW_POLL_MS);
  };

  const togglePlatform = (p: MhPlatform) =>
    setPlatforms((cur) => (cur.includes(p) ? cur.filter((x) => x !== p) : PLATFORMS.filter((x) => x === p || cur.includes(x))));

  const handlePreview = async () => {
    setPreviewing(true);
    setConfirming(false);
    try {
      const r = await previewMhRelease(req);
      setPreview(r);
      if (MH_ACTIVE_STATES.includes(r.state)) pollPreview(r.id);
    } catch (e) {
      notify(mhErrorDetail(e), "error");
    } finally {
      setPreviewing(false);
    }
  };

  const handleRelease = async () => {
    setSubmitting(true);
    try {
      const r = await startMhRelease(req);
      notify(`${t("发版已触发")}: #${r.id} ${r.module}`, "success");
      setConfirming(false);
      onStarted(r);
    } catch (e) {
      notify(mhErrorDetail(e), "error");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section className="rounded-lg p-5 j-rise" style={{ ["--d" as string]: "0.06s", background: S.overlay, border: `1px solid ${S.border}` }}>
      <h2 className="text-base font-semibold">{t("发起模块发版")}</h2>
      <p className="mt-1 text-xs" style={{ color: S.text2 }}>
        {t("一次发版 = 勾选平台共用一个版本号、一个 tag；建议先预览（dry run）确认版本号与 API 变更。")}
      </p>

      <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-12 sm:items-end">
        <div className="sm:col-span-4">
          <label className="block text-xs" style={{ color: S.text2 }}>{t("模块")}</label>
          <input
            type="text"
            value={module}
            onChange={(e) => onModuleChange(e.target.value.trim())}
            list="mh-module-options"
            placeholder="logger"
            className="mt-1 w-full rounded px-3 py-2 text-sm"
            style={inputStyle}
            autoComplete="off"
          />
          <datalist id="mh-module-options">
            {modules.map((m) => <option key={m.name} value={m.name} />)}
          </datalist>
        </div>
        <div className="sm:col-span-4">
          <label className="block text-xs" style={{ color: S.text2 }}>{t("分支（main 或 release/*）")}</label>
          <input
            type="text"
            value={branch}
            onChange={(e) => setBranch(e.target.value.trim())}
            placeholder="main"
            className="mt-1 w-full rounded px-3 py-2 text-sm"
            style={{ ...inputStyle, borderColor: branch && !branchOk ? S.danger : S.border }}
            autoComplete="off"
          />
        </div>
        <div className="flex flex-wrap items-center gap-4 sm:col-span-4 sm:pb-2">
          {PLATFORMS.map((p) => (
            <label key={p} className="flex items-center gap-1.5 text-sm" style={{ color: S.text1 }}>
              <input type="checkbox" checked={platforms.includes(p)} onChange={() => togglePlatform(p)} />
              {PLATFORM_LABEL[p]}
            </label>
          ))}
          <label className="flex items-center gap-1.5 text-sm" style={{ color: S.text1 }}>
            <input type="checkbox" checked={major} onChange={(e) => setMajor(e.target.checked)} />
            {t("major 升级")}
          </label>
        </div>
      </div>

      <div className="mt-2 space-y-1 text-xs">
        {current && (
          <div style={{ color: S.text3 }}>
            {current.repo} · {t("main 当前固定版本")}:{" "}
            {PLATFORMS.map((p) => `${PLATFORM_LABEL[p]} ${current.platforms[p] || "—"}`).join(" / ")}
          </div>
        )}
        {branch && !branchOk && <div style={{ color: S.danger }}>{t("分支必须是 main 或 release/*")}</div>}
        {platforms.length === 0 && <div style={{ color: S.danger }}>{t("至少勾选一个平台")}</div>}
        {majorBlocked && <div style={{ color: S.danger }}>{t("release/* 分支不允许 major 升级")}</div>}
      </div>

      <div className="mt-4 flex flex-wrap gap-2">
        <button
          onClick={handlePreview}
          disabled={!valid || previewing || previewActive}
          className="rounded px-4 py-2 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50"
          style={{ border: `1px solid ${S.accent}`, color: S.accent }}
        >
          {previewing || previewActive ? t("预览中…") : t("预览（dry run）")}
        </button>
        <button
          onClick={() => setConfirming(true)}
          disabled={!valid || submitting || confirming}
          className="rounded px-4 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:opacity-50"
          style={{ background: S.accent }}
        >
          {t("发版")}
        </button>
      </div>

      {confirming && (
        <div className="mt-4 rounded p-3 text-sm" style={{ background: S.warnBg, color: S.warn, border: `1px solid ${S.border}` }}>
          <div className="font-medium">
            {t("确认发版")}: {module} · {branch} · {platforms.map((p) => PLATFORM_LABEL[p]).join(" + ")}
            {major ? " · major" : ""}
            {previewMatches && preview?.version ? ` → ${preview.version}` : ""}
          </div>
          <div className="mt-1 text-xs">
            {t("将触发 Jenkins 发布任务（上传制品、打 tag），成功后自动给壳工程开 bump PR。")}
            {!previewMatches && ` ${t("当前参数还没有预览过。")}`}
          </div>
          <div className="mt-2 flex gap-2">
            <button
              onClick={handleRelease}
              disabled={submitting}
              className="rounded px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
              style={{ background: S.danger }}
            >
              {submitting ? t("提交中…") : t("确认发版")}
            </button>
            <button onClick={() => setConfirming(false)} className="rounded px-3 py-1.5 text-xs" style={{ border: `1px solid ${S.border}`, color: S.text2 }}>
              {t("取消")}
            </button>
          </div>
        </div>
      )}

      {preview && <PreviewResult preview={preview} current={current} stale={!previewMatches} />}
    </section>
  );
}

function PreviewResult({ preview, current, stale }: { preview: MhRelease; current?: MhModule; stale: boolean }) {
  const t = useT();
  const st = STATE_STYLE[preview.state] || STATE_STYLE.pending;
  const active = MH_ACTIVE_STATES.includes(preview.state);
  const stalled = active && sinceSeconds(preview.createdAt) > STALL_HINT_SECONDS;
  return (
    <div className="mt-4 rounded p-3 text-xs" style={{ background: S.accentBg, border: `1px solid ${S.border}`, opacity: stale ? 0.6 : 1 }}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium" style={{ color: S.text1 }}>{t("预览")} #{preview.id}</span>
        <span className="rounded px-2 py-0.5" style={{ background: st.bg, color: st.fg }}>{t(st.label)}</span>
        {preview.buildUrl && <a href={preview.buildUrl} target="_blank" rel="noreferrer" style={{ color: S.accent }}>Jenkins</a>}
        {stale && <span style={{ color: S.text3 }}>{t("（参数已改动，此预览不再对应当前表单）")}</span>}
      </div>
      {stalled && <div className="mt-2" style={{ color: S.warn }}>{t("预览长时间停在构建中：若 modulehub 未启用（enabled=false），后台轮询不会运行，状态不会更新。")}</div>}
      {preview.state === "failed" && <div className="mt-2" style={{ color: S.danger }}>{preview.error || t("失败")}</div>}
      {preview.version && (
        <div className="mt-2 space-y-2">
          <div style={{ color: S.text1 }}>
            {t("下一个版本")}: <span className="font-mono font-semibold">{preview.version}</span>
          </div>
          {preview.platforms.map((p) => {
            const a = preview.artifacts[p];
            return (
              <div key={p}>
                <div style={{ color: S.text2 }}>
                  {PLATFORM_LABEL[p]}: <span className="font-mono">{current?.platforms[p] || "—"} → {preview.version}</span>
                  {a?.coordinate && <span className="ml-2 font-mono" style={{ color: S.text3 }}>{a.coordinate}</span>}
                </div>
                {a && a.apiChanges.length > 0 ? (
                  <ul className="ml-4 mt-1 list-disc font-mono" style={{ color: S.text2 }}>
                    {a.apiChanges.map((c, i) => <li key={i}>{c}</li>)}
                  </ul>
                ) : (
                  <div className="ml-4" style={{ color: S.text3 }}>{t("无 API 变更")}</div>
                )}
              </div>
            );
          })}
          <div style={{ color: S.text3 }}>{t("changelog 由后端写进 bump PR 描述，预览接口不返回。")}</div>
        </div>
      )}
    </div>
  );
}
