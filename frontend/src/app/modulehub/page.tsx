"use client";

import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Toast } from "@/components/Toast";
import { useT } from "@/lib/i18n";
import {
  ApiError,
  listMhModules,
  listMhReleases,
  mhErrorDetail,
  resumeMhRelease,
  MH_ACTIVE_STATES,
  type MhModule,
  type MhRelease,
} from "@/lib/api";
import { ReleaseForm } from "./ReleaseForm";
import { ReleasesTable } from "./ReleasesTable";
import { MirrorPanel } from "./MirrorPanel";
import { S, PLATFORMS, PLATFORM_LABEL, sinceSeconds } from "./ui";

const ACTIVE_REFRESH_MS = 10_000;
// An active release whose row has not been touched for this long suggests the poller is not running.
const STALE_ACTIVE_SECONDS = 180;

function loadErrorText(e: unknown, t: (k: string) => string): string {
  if (e instanceof ApiError && e.status === 404) return t("后端没有挂载 modulehub 接口（/api/modulehub）");
  return mhErrorDetail(e);
}

export default function ModulehubPage() {
  const t = useT();
  return (
    <Suspense fallback={<div className="p-8" style={{ color: S.text2 }}>{t("加载中...")}</div>}>
      <ModulehubPageInner />
    </Suspense>
  );
}

function ModulehubPageInner() {
  const t = useT();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  // URL is the source of truth for the deep-linkable bits (?module=&release=&previews=1)
  // module is typed into an input: keep it in local state (URL updates are async) and mirror it to the URL
  const [module, setModuleState] = useState(searchParams?.get("module") || "");
  const releaseParam = parseInt(searchParams?.get("release") || "", 10);
  const selectedId = Number.isFinite(releaseParam) ? releaseParam : null;
  const showPreviews = searchParams?.get("previews") === "1";

  const updateQuery = useCallback(
    (patch: Record<string, string | null>) => {
      const params = new URLSearchParams(Array.from(searchParams?.entries() || []));
      for (const [k, v] of Object.entries(patch)) {
        if (v) params.set(k, v);
        else params.delete(k);
      }
      const qs = params.toString();
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    },
    [router, pathname, searchParams],
  );

  const [modules, setModules] = useState<MhModule[]>([]);
  const [modulesError, setModulesError] = useState<string | null>(null);
  const [loadingModules, setLoadingModules] = useState(false);
  const [releases, setReleases] = useState<MhRelease[]>([]);
  const [releasesError, setReleasesError] = useState<string | null>(null);
  const [loadingReleases, setLoadingReleases] = useState(false);
  const [resumingId, setResumingId] = useState<number | null>(null);
  const [toast, setToast] = useState<{ msg: string; type: "success" | "error" } | null>(null);
  const notify = useCallback((msg: string, type: "success" | "error") => setToast({ msg, type }), []);
  const setModule = (m: string) => {
    setModuleState(m);
    updateQuery({ module: m || null });
  };

  const loadModules = useCallback(async () => {
    setLoadingModules(true);
    try {
      setModules(await listMhModules());
      setModulesError(null);
    } catch (e) {
      setModulesError(loadErrorText(e, t));
    } finally {
      setLoadingModules(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadReleases = useCallback(async () => {
    setLoadingReleases(true);
    try {
      setReleases(await listMhReleases(100));
      setReleasesError(null);
    } catch (e) {
      setReleasesError(loadErrorText(e, t));
    } finally {
      setLoadingReleases(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    loadModules();
    loadReleases();
  }, [loadModules, loadReleases]);

  const anyActive = releases.some((r) => MH_ACTIVE_STATES.includes(r.state));
  // Auto refresh only while something is in flight
  useEffect(() => {
    if (!anyActive) return;
    const id = setInterval(loadReleases, ACTIVE_REFRESH_MS);
    return () => clearInterval(id);
  }, [anyActive, loadReleases]);

  const visible = useMemo(
    () => releases.filter((r) => showPreviews || r.kind === "release" || r.id === selectedId),
    [releases, showPreviews, selectedId],
  );
  const stale = releases.some(
    (r) => MH_ACTIVE_STATES.includes(r.state) && sinceSeconds(r.updatedAt || r.createdAt) > STALE_ACTIVE_SECONDS,
  );

  const handleResume = async (r: MhRelease) => {
    if (!window.confirm(`${t("续跑发版")} #${r.id} ${r.module} ${r.version} (${r.failedFrom})?`)) return;
    setResumingId(r.id);
    try {
      await resumeMhRelease(r.id);
      notify(`${t("已续跑")} #${r.id}`, "success");
      await loadReleases();
    } catch (e) {
      notify(mhErrorDetail(e), "error");
    } finally {
      setResumingId(null);
    }
  };

  const handleStarted = async (r: MhRelease) => {
    updateQuery({ release: String(r.id) });
    await loadReleases();
  };

  return (
    <div className="min-h-screen p-6" style={{ background: S.surface, color: S.text1 }}>
      {toast && <Toast msg={toast.msg} type={toast.type} onClose={() => setToast(null)} />}

      <div className="mx-auto max-w-6xl space-y-6">
        <div className="j-rise">
          <h1 className="text-xl font-semibold">{t("模块发版")}</h1>
          <p className="mt-1 text-sm" style={{ color: S.text2 }}>
            {t("独立 native 模块（Android AAR / iOS XCFramework）发版：触发发布任务 → 打 tag → 给两个壳工程开 bump PR。")}
          </p>
        </div>

        {/* ─── modules ─────────────────────────────────────────── */}
        <section className="rounded-lg p-5 j-rise" style={{ background: S.overlay, border: `1px solid ${S.border}` }}>
          <div className="flex items-center justify-between">
            <h2 className="text-base font-semibold">{t("模块")}</h2>
            <button onClick={loadModules} className="text-xs" style={{ color: S.text2 }}>
              {loadingModules ? t("刷新中…") : t("刷新")}
            </button>
          </div>
          <p className="mt-1 text-xs" style={{ color: S.text2 }}>{t("各壳工程 main 上 modules.versions.toml 固定的版本；点一行带入发版表单。")}</p>
          {modulesError && <ErrorBanner text={modulesError} onRetry={loadModules} />}
          {!modulesError && (
            <table className="mt-3 min-w-full text-xs">
              <thead>
                <tr style={{ color: S.text2 }}>
                  <th className="px-2 py-2 text-left">{t("模块")}</th>
                  <th className="px-2 py-2 text-left">{t("仓库")}</th>
                  {PLATFORMS.map((p) => <th key={p} className="px-2 py-2 text-left">{PLATFORM_LABEL[p]}</th>)}
                </tr>
              </thead>
              <tbody>
                {modules.length === 0 && (
                  <tr>
                    <td colSpan={4} className="px-2 py-6 text-center" style={{ color: S.text3 }}>
                      {loadingModules ? t("加载中...") : t("壳工程里没有登记模块")}
                    </td>
                  </tr>
                )}
                {modules.map((m) => (
                  <tr
                    key={m.name}
                    onClick={() => setModule(m.name)}
                    className="cursor-pointer"
                    style={{ borderTop: `1px solid ${S.border}`, background: m.name === module ? S.accentBg : "transparent" }}
                  >
                    <td className="px-2 py-2 font-medium">{m.name}</td>
                    <td className="px-2 py-2 font-mono" style={{ color: S.text2 }}>{m.repo || "—"}</td>
                    {PLATFORMS.map((p) => <td key={p} className="px-2 py-2 font-mono">{m.platforms[p] || "—"}</td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        <ReleaseForm
          modules={modules}
          module={module}
          onModuleChange={setModule}
          onStarted={handleStarted}
          notify={notify}
        />

        {/* ─── releases ────────────────────────────────────────── */}
        <section className="rounded-lg p-5" style={{ background: S.overlay, border: `1px solid ${S.border}` }}>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-base font-semibold">{t("发版记录")}</h2>
            <div className="flex items-center gap-4 text-xs" style={{ color: S.text2 }}>
              <label className="flex items-center gap-1.5">
                <input type="checkbox" checked={showPreviews} onChange={(e) => updateQuery({ previews: e.target.checked ? "1" : null })} />
                {t("显示预览记录")}
              </label>
              {anyActive && <span style={{ color: S.accent }}>{t("进行中，每 10 秒自动刷新")}</span>}
              <button onClick={loadReleases}>{loadingReleases ? t("刷新中…") : t("立即刷新")}</button>
            </div>
          </div>
          {stale && (
            <div className="mt-2 rounded p-2 text-xs" style={{ background: S.warnBg, color: S.warn }}>
              {t("有进行中的发版超过 3 分钟没有更新：modulehub 默认不启用（enabled=false），未启用时后台轮询不运行，状态不会推进。")}
            </div>
          )}
          {releasesError ? (
            <ErrorBanner text={releasesError} onRetry={loadReleases} />
          ) : (
            <ReleasesTable
              releases={visible}
              selectedId={selectedId}
              onSelect={(id) => updateQuery({ release: id === null ? null : String(id) })}
              onResume={handleResume}
              resumingId={resumingId}
            />
          )}
        </section>

        <MirrorPanel notify={notify} />
      </div>
    </div>
  );
}

function ErrorBanner({ text, onRetry }: { text: string; onRetry: () => void }) {
  const t = useT();
  return (
    <div className="mt-3 flex items-start justify-between gap-3 rounded p-3 text-xs" style={{ background: S.dangerBg, color: S.danger }}>
      <span className="break-all">{t("加载失败")}: {text}</span>
      <button onClick={onRetry} className="flex-shrink-0 font-medium underline">{t("重试")}</button>
    </div>
  );
}
