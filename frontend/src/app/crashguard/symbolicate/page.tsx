"use client";

/**
 * Ad-hoc 堆栈符号化工作台（2026-09-22）
 *
 * 链路：粘贴堆栈 → inspect 预填（只预填不锁定）→ 平台/版本可改 → preflight 预检
 * → 符号化 → 原始/符号化对比 + 逐帧诊断。底部挂符号包上传框（missing 档高亮指向它）。
 *
 * 设计要点（别随手改坏）：
 * - inspect 的结果只做「预填」。用户手动改过 platform / app_version 之后，
 *   后续 inspect 不再覆盖（_touched ref 兜住），否则边打字边被解析结果顶回去。
 * - frame_stats 的 app_frames / app_symbolicated / non_app_frames 可能是 null
 *   （推断不出 App module）。null 必须降级成粗粒度展示，**绝不能渲染成 0** ——
 *   那会让人误判成「App 帧一个都没解出来」。
 * - warnings 空数组时不渲染板块（铁律 feedback_report_only_anomalies：有问题才显示）。
 */

import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import Link from "next/link";
import { useT } from "@/lib/i18n";
import {
  inspectStack,
  listSymbolicateVersions,
  preflightSymbolicate,
  symbolicateStack,
  uploadSymbolPackage,
  type StackInsight,
  type SymbolPreflight,
  type SymbolicateResult,
  type VersionCandidate,
} from "@/lib/api";

// 主题 token：可主题化的一律走 var(--j-*)。
// ok/warn/danger 三个语义色全站（crashguard/page.tsx、jobs/page.tsx）都是这三个定值，
// 明暗两套主题下都刻意保持一致，这里跟随既有约定，不另造一套。
const D = {
  bg: "var(--j-surface)",
  surface: "var(--j-panel)",
  surfaceAlt: "var(--j-surface)",
  border: "var(--j-border)",
  borderSm: "var(--j-border-sm)",
  text1: "var(--j-ink)",
  text2: "var(--j-graphite)",
  text3: "var(--j-faint)",
  accent: "var(--j-accent)",
  accentSoft: "var(--j-accent-soft)",
  hover: "var(--j-hover)",
  ok: "#16A34A",
  okBg: "rgba(22,163,74,0.10)",
  warn: "#D97706",
  warnBg: "rgba(217,119,6,0.10)",
  danger: "#DC2626",
  dangerBg: "rgba(220,38,38,0.10)",
  info: "#2563EB",
  infoBg: "rgba(37,99,235,0.10)",
  muted: "#6B7280",
  mutedBg: "rgba(107,114,128,0.12)",
} as const;

const MONO = "var(--font-mono)";

const PLATFORM_LABEL: Record<string, string> = { ios: "iOS", android: "Android", flutter: "Flutter" };

const FORMAT_LABEL: Record<string, string> = {
  apple_ips_json: "Apple .ips (JSON)",
  apple_crash_text: "Apple .crash",
  datadog_rum: "Datadog RUM",
  android_java: "Android Java",
  android_tombstone: "Android tombstone",
  android_logcat: "Android logcat",
  unknown: "—",
};

const SOURCE_BADGE: Record<VersionCandidate["source"], { fg: string; bg: string; label: string }> = {
  cached: { fg: D.ok, bg: D.okBg, label: "已缓存" },
  uploaded: { fg: D.info, bg: D.infoBg, label: "已上传" },
  release: { fg: D.muted, bg: D.mutedBg, label: "Release" },
};

const STATUS_STYLE: Record<SymbolPreflight["status"], { fg: string; bg: string; icon: string }> = {
  cached: { fg: D.ok, bg: D.okBg, icon: "🟢" },
  available: { fg: D.warn, bg: D.warnBg, icon: "🟡" },
  missing: { fg: D.danger, bg: D.dangerBg, icon: "🔴" },
};

const SYMBOL_TYPES = ["dsym", "dart_symbols", "proguard_mapping", "native_symbols"] as const;

function errMsg(e: unknown): string {
  const m = (e as { message?: string })?.message || String(e);
  return m.length > 240 ? m.slice(0, 240) + "…" : m;
}

function humanBytes(n: number): string {
  if (!n || n <= 0) return "—";
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/** 单侧堆栈面板：左侧行号槽 + 右侧可横向滚动的 <pre>，两侧共用行高保证行号对齐。 */
function StackPane({ title, text, tone }: { title: string; text: string; tone: string }) {
  const lines = useMemo(() => (text ? text.split("\n") : []), [text]);
  return (
    <div
      style={{
        background: D.surface,
        border: `1px solid ${D.border}`,
        borderRadius: 8,
        overflow: "hidden",
        minWidth: 0,
      }}
    >
      <div
        style={{
          padding: "8px 12px",
          borderBottom: `1px solid ${D.borderSm}`,
          fontSize: 12,
          fontWeight: 600,
          color: tone,
          display: "flex",
          justifyContent: "space-between",
          gap: 8,
        }}
      >
        <span>{title}</span>
        <span style={{ color: D.text3, fontWeight: 400 }}>{lines.length} L</span>
      </div>
      {/* 纵向滚动放在外层（行号槽与 pre 一起动），横向滚动放在 pre 内部 */}
      <div style={{ display: "flex", maxHeight: 460, overflowY: "auto" }}>
        <div
          aria-hidden
          style={{
            flex: "0 0 auto",
            padding: "10px 6px 10px 10px",
            textAlign: "right",
            fontFamily: MONO,
            fontSize: 11,
            lineHeight: "18px",
            color: D.text3,
            background: D.surfaceAlt,
            borderRight: `1px solid ${D.borderSm}`,
            userSelect: "none",
          }}
        >
          {lines.map((_, i) => (
            <div key={i}>{i + 1}</div>
          ))}
        </div>
        <pre
          style={{
            flex: "1 1 0",
            minWidth: 0,
            margin: 0,
            padding: "10px 12px",
            fontFamily: MONO,
            fontSize: 11,
            lineHeight: "18px",
            color: D.text1,
            whiteSpace: "pre",
            overflowX: "auto",
          }}
        >
          {text}
        </pre>
      </div>
    </div>
  );
}

/** 解析结果条里的一个字段：值 + 来源标注。 */
function InsightField({ label, value, origin }: { label: string; value: string; origin: string }) {
  return (
    <div
      style={{
        border: `1px solid ${D.borderSm}`,
        background: D.surfaceAlt,
        borderRadius: 6,
        padding: "6px 10px",
        minWidth: 0,
      }}
    >
      <div style={{ fontSize: 10, color: D.text3, marginBottom: 2 }}>{label}</div>
      <div style={{ fontSize: 12, fontWeight: 600, color: D.text1, wordBreak: "break-all" }}>{value}</div>
      <div style={{ fontSize: 10, color: D.text3, marginTop: 2 }}>{origin}</div>
    </div>
  );
}

export default function SymbolicateWorkbenchPage() {
  const t = useT();

  const [stack, setStack] = useState("");
  const [insight, setInsight] = useState<StackInsight | null>(null);
  const [insightLoading, setInsightLoading] = useState(false);
  const [insightError, setInsightError] = useState<string | null>(null);

  const [platform, setPlatform] = useState("");
  const [appVersion, setAppVersion] = useState("");
  // 用户一旦手动改过，inspect 就不再覆盖（用 ref 避免把它们塞进 debounce 的依赖里
  // ——否则每次「触碰」都会重跑一次 inspect）
  const platformTouched = useRef(false);
  const versionTouched = useRef(false);

  const [versions, setVersions] = useState<VersionCandidate[]>([]);
  const [versionWarnings, setVersionWarnings] = useState<string[]>([]);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [comboOpen, setComboOpen] = useState(false);
  const comboBlurTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const [preflight, setPreflight] = useState<SymbolPreflight | null>(null);
  const [preflightLoading, setPreflightLoading] = useState(false);
  const [preflightError, setPreflightError] = useState<string | null>(null);

  const [running, setRunning] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const elapsedTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const [result, setResult] = useState<SymbolicateResult | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const [confirmMissing, setConfirmMissing] = useState(false);

  const uploadRef = useRef<HTMLDivElement | null>(null);
  const [uploadFlash, setUploadFlash] = useState(false);
  const [upPlatform, setUpPlatform] = useState("ios");
  const [upVersion, setUpVersion] = useState("");
  const [upType, setUpType] = useState<string>("dsym");
  const [upFile, setUpFile] = useState<File | null>(null);
  const [upBusy, setUpBusy] = useState(false);
  const [upMsg, setUpMsg] = useState<string | null>(null);
  const [upErr, setUpErr] = useState<string | null>(null);

  // ── inspect：debounce 600ms ────────────────────────────────────────────────
  useEffect(() => {
    if (!stack.trim()) {
      setInsight(null);
      setInsightError(null);
      return;
    }
    const h = setTimeout(async () => {
      setInsightLoading(true);
      try {
        const r = await inspectStack(stack);
        setInsight(r);
        setInsightError(null);
        if (!platformTouched.current && r.platform) setPlatform(r.platform);
        if (!versionTouched.current && r.app_version) setAppVersion(r.app_version);
      } catch (e) {
        setInsight(null);
        setInsightError(errMsg(e));
      } finally {
        setInsightLoading(false);
      }
    }, 600);
    return () => clearTimeout(h);
  }, [stack]);

  // ── 版本候选：平台确定后拉一次 ─────────────────────────────────────────────
  useEffect(() => {
    if (platform !== "ios" && platform !== "android") {
      setVersions([]);
      setVersionWarnings([]);
      return;
    }
    let alive = true;
    setVersionsLoading(true);
    listSymbolicateVersions(platform)
      .then((r) => {
        if (!alive) return;
        setVersions(r.versions || []);
        setVersionWarnings(r.warnings || []);
      })
      .catch((e) => {
        if (!alive) return;
        setVersions([]);
        setVersionWarnings([errMsg(e)]);
      })
      .finally(() => {
        if (alive) setVersionsLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [platform]);

  // 平台变了顺手把上传框的平台/符号类型带过去（用户仍可改）
  useEffect(() => {
    if (platform === "ios" || platform === "android") {
      setUpPlatform(platform);
      setUpType(platform === "ios" ? "dsym" : "proguard_mapping");
    }
  }, [platform]);

  // ── preflight：平台 + 版本都有值就自动预检（debounce 400ms，避免手打时刷接口）──
  useEffect(() => {
    const v = appVersion.trim();
    setConfirmMissing(false);
    if ((platform !== "ios" && platform !== "android") || !v) {
      setPreflight(null);
      setPreflightError(null);
      return;
    }
    let alive = true;
    const h = setTimeout(async () => {
      setPreflightLoading(true);
      try {
        const r = await preflightSymbolicate({
          platform,
          app_version: v,
          symbol_profile: insight?.routing?.symbol_profile || undefined,
          github_repo: insight?.routing?.github_repo || undefined,
        });
        if (!alive) return;
        setPreflight(r);
        setPreflightError(null);
      } catch (e) {
        if (!alive) return;
        setPreflight(null);
        setPreflightError(errMsg(e));
      } finally {
        if (alive) setPreflightLoading(false);
      }
    }, 400);
    return () => {
      alive = false;
      clearTimeout(h);
    };
  }, [platform, appVersion, insight?.routing?.symbol_profile, insight?.routing?.github_repo]);

  // 计时器只在卸载时清一次（提交流程自己在 finally 里停）
  useEffect(() => {
    return () => {
      if (elapsedTimer.current) clearInterval(elapsedTimer.current);
      if (comboBlurTimer.current) clearTimeout(comboBlurTimer.current);
    };
  }, []);

  const jumpToUpload = useCallback(() => {
    uploadRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
    setUploadFlash(true);
    setTimeout(() => setUploadFlash(false), 2200);
  }, []);

  const onSubmit = useCallback(async () => {
    if (running) return;
    if (!stack.trim()) {
      setRunError(t("请先粘贴堆栈"));
      return;
    }
    if (platform !== "ios" && platform !== "android" && platform !== "flutter") {
      setRunError(t("请先选择平台"));
      return;
    }
    // missing 档二次确认：第一次点击只把按钮变成确认态
    if (preflight?.status === "missing" && !confirmMissing) {
      setConfirmMissing(true);
      return;
    }
    setRunning(true);
    setRunError(null);
    setResult(null);
    setElapsed(0);
    if (elapsedTimer.current) clearInterval(elapsedTimer.current);
    // 已耗时秒数：1s 心跳的 setInterval（不用 requestAnimationFrame——这里只需要
    // 秒级粒度，且页面切到后台时不刷也无所谓）
    elapsedTimer.current = setInterval(() => setElapsed((n) => n + 1), 1000);
    try {
      const r = await symbolicateStack({
        stack,
        platform,
        app_version: appVersion.trim() || undefined,
        symbol_profile: insight?.routing?.symbol_profile || undefined,
        github_repo: insight?.routing?.github_repo || undefined,
      });
      setResult(r);
      setConfirmMissing(false);
    } catch (e) {
      setRunError(errMsg(e));
    } finally {
      if (elapsedTimer.current) {
        clearInterval(elapsedTimer.current);
        elapsedTimer.current = null;
      }
      setRunning(false);
    }
  }, [running, stack, platform, appVersion, preflight, confirmMissing, insight, t]);

  const onUpload = useCallback(async () => {
    if (upBusy) return;
    setUpErr(null);
    setUpMsg(null);
    if (!upFile) {
      setUpErr(t("请选择符号包文件"));
      return;
    }
    if (!upVersion.trim()) {
      setUpErr(t("请填写版本号"));
      return;
    }
    setUpBusy(true);
    try {
      const r = await uploadSymbolPackage(upPlatform, upVersion.trim(), upType, upFile);
      setUpMsg(`${t("上传成功")} · ${r.symbol_type} · ${humanBytes(r.size_bytes)}`);
      // 上传的正是当前要查的版本 → 立刻重跑 preflight（状态应从 missing 翻成 uploaded/cached）
      if (upPlatform === platform && upVersion.trim() === appVersion.trim()) {
        try {
          const pf = await preflightSymbolicate({ platform: upPlatform, app_version: upVersion.trim() });
          setPreflight(pf);
        } catch {
          /* 预检刷新失败不影响上传结果展示 */
        }
      }
    } catch (e) {
      setUpErr(errMsg(e));
    } finally {
      setUpBusy(false);
    }
  }, [upBusy, upFile, upVersion, upPlatform, upType, platform, appVersion, t]);

  const filteredVersions = useMemo(() => {
    const q = appVersion.trim().toLowerCase();
    if (!q) return versions.slice(0, 40);
    return versions.filter((v) => v.app_version.toLowerCase().includes(q)).slice(0, 40);
  }, [versions, appVersion]);

  const selectedCandidate = useMemo(
    () => versions.find((v) => v.app_version === appVersion.trim()) || null,
    [versions, appVersion],
  );

  // frame_stats → 人话。app_module 推断不出时只给粗粒度，不把 null 当 0。
  const frameStatsLine = useMemo(() => {
    if (!result) return "";
    const fs = result.frame_stats;
    const fine = !!fs.app_module && fs.app_frames !== null && fs.app_symbolicated !== null;
    if (fine) {
      const appFrames = fs.app_frames as number;
      const appSym = fs.app_symbolicated as number;
      const failed = Math.max(0, appFrames - appSym);
      const nonApp = fs.non_app_frames === null ? Math.max(0, fs.total_frames - appFrames) : fs.non_app_frames;
      return (
        `${t("总帧数")} ${fs.total_frames} · ${fs.app_module} ${t("帧")} ${appFrames}` +
        `（${t("符号化")} ${appSym} / ${t("失败")} ${failed}）` +
        ` · ${t("其他模块")} ${nonApp}（${t("按设计跳过")}）`
      );
    }
    return `${t("总帧数")} ${fs.total_frames} · ${t("已符号化")} ${fs.symbolicated} · ${t("未解析")} ${fs.unresolved}`;
  }, [result, t]);

  const btnBase: CSSProperties = {
    borderRadius: 6,
    fontSize: 12,
    fontWeight: 600,
    padding: "6px 12px",
    border: `1px solid ${D.border}`,
    background: "transparent",
    color: D.text1,
    cursor: "pointer",
  };

  const inputStyle: CSSProperties = {
    width: "100%",
    boxSizing: "border-box",
    background: D.surface,
    border: `1px solid ${D.border}`,
    borderRadius: 6,
    padding: "7px 10px",
    fontSize: 12,
    color: D.text1,
    fontFamily: MONO,
  };

  const cardStyle: CSSProperties = {
    background: D.surface,
    border: `1px solid ${D.border}`,
    borderRadius: 8,
    padding: 16,
    marginBottom: 16,
  };

  return (
    <div style={{ background: D.bg, minHeight: "100vh", color: D.text1 }}>
      <div style={{ maxWidth: 1240, margin: "0 auto" }} className="px-4 py-6 sm:px-6">
        {/* Header */}
        <div
          className="j-rise"
          style={{ display: "flex", flexWrap: "wrap", gap: 12, justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}
        >
          <div style={{ minWidth: 0 }}>
            <h1 style={{ fontSize: 22, fontWeight: 600 }}>🧩 {t("堆栈符号化")}</h1>
            <p style={{ fontSize: 13, color: D.text2, marginTop: 4 }}>
              {t("粘贴任意崩溃堆栈，自动识别平台/格式/版本，符号化前先做符号表预检")}
            </p>
          </div>
          <Link href="/crashguard" style={{ color: D.accent, fontSize: 13, textDecoration: "none" }}>
            ← {t("返回主页")}
          </Link>
        </div>

        {/* 1. 输入区 */}
        <div style={cardStyle}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 8, gap: 8, flexWrap: "wrap" }}>
            <span style={{ fontSize: 13, fontWeight: 600 }}>{t("粘贴堆栈")}</span>
            <span style={{ fontSize: 11, color: D.text3 }}>
              {insightLoading ? t("解析中…") : `${stack.length} ${t("字符")}`}
            </span>
          </div>
          <textarea
            value={stack}
            onChange={(e) => setStack(e.target.value)}
            spellCheck={false}
            placeholder={t(
              "支持：Apple .ips / .crash 崩溃报告、Datadog 堆栈、Android Java 异常栈、Android tombstone / logcat。直接整段粘贴即可。",
            )}
            style={{
              width: "100%",
              boxSizing: "border-box",
              minHeight: 240,
              resize: "vertical",
              background: D.surfaceAlt,
              border: `1px solid ${D.border}`,
              borderRadius: 6,
              padding: 12,
              fontFamily: MONO,
              fontSize: 12,
              lineHeight: "18px",
              color: D.text1,
            }}
          />
          {insightError && (
            <div style={{ marginTop: 8, fontSize: 12, color: D.danger }}>
              {t("解析失败")}：{insightError}
            </div>
          )}
        </div>

        {/* 2. 解析结果条 */}
        {insight && (
          <div style={cardStyle}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 10, gap: 8, flexWrap: "wrap" }}>
              <span style={{ fontSize: 13, fontWeight: 600 }}>{t("解析结果")}</span>
              <span style={{ fontSize: 11, color: D.text3 }}>
                {t("解析只预填，不锁定——下面每一项都能手改")}
              </span>
            </div>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
              <InsightField
                label={t("平台")}
                value={PLATFORM_LABEL[insight.platform] || insight.platform || "—"}
                origin={insight.platform ? t("从堆栈解析") : t("未识别")}
              />
              <InsightField
                label={t("格式")}
                value={FORMAT_LABEL[insight.stack_format] || insight.stack_format}
                origin={`${t("置信度")} ${insight.confidence}`}
              />
              <InsightField
                label={t("版本号")}
                value={insight.app_version || "—"}
                origin={insight.app_version ? t("从堆栈解析") : t("堆栈中不含，需手填")}
              />
              <InsightField label={t("帧数")} value={String(insight.frame_count)} origin={t("解析计数")} />
              <InsightField
                label="UUID / Build ID"
                value={String((insight.uuids?.length || 0) + (insight.build_ids?.length || 0))}
                origin={t("用于符号匹配")}
              />
              <InsightField
                label={t("路由")}
                value={insight.routing ? insight.routing.symbol_profile : "—"}
                origin={insight.routing ? `${insight.routing.github_repo} · ${insight.routing.confidence}` : t("需版本号才能路由")}
              />
            </div>
            {insight.notes?.length > 0 && (
              <ul style={{ margin: "10px 0 0", paddingLeft: 18, fontSize: 12, color: D.text2, lineHeight: 1.7 }}>
                {insight.notes.map((n, i) => (
                  <li key={i}>{n}</li>
                ))}
              </ul>
            )}
          </div>
        )}

        {/* 3 + 4. 平台 radio + 版本 combobox */}
        <div style={cardStyle}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 10 }}>{t("符号化参数")}</div>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div>
              <div style={{ fontSize: 11, color: D.text3, marginBottom: 6 }}>{t("平台")}</div>
              <div style={{ display: "flex", gap: 14, flexWrap: "wrap" }}>
                {["ios", "android"].map((p) => (
                  <label key={p} style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12, cursor: "pointer" }}>
                    <input
                      type="radio"
                      name="platform"
                      checked={platform === p}
                      onChange={() => {
                        platformTouched.current = true;
                        setPlatform(p);
                      }}
                    />
                    {PLATFORM_LABEL[p]}
                  </label>
                ))}
              </div>
              {insight?.platform && platform !== insight.platform && (
                <div style={{ fontSize: 11, color: D.warn, marginTop: 6 }}>
                  ⚠ {t("与解析结果不一致")}（{PLATFORM_LABEL[insight.platform] || insight.platform}）
                </div>
              )}
            </div>

            <div style={{ position: "relative" }}>
              <div style={{ fontSize: 11, color: D.text3, marginBottom: 6, display: "flex", justifyContent: "space-between", gap: 8 }}>
                <span>{t("版本号")}</span>
                <span>
                  {versionsLoading
                    ? t("加载候选…")
                    : platform
                      ? `${versions.length} ${t("个候选")}`
                      : t("先选平台")}
                </span>
              </div>
              <input
                value={appVersion}
                onChange={(e) => {
                  versionTouched.current = true;
                  setAppVersion(e.target.value);
                  setComboOpen(true);
                }}
                onFocus={() => {
                  if (comboBlurTimer.current) clearTimeout(comboBlurTimer.current);
                  setComboOpen(true);
                }}
                onBlur={() => {
                  // 延迟关闭，否则点候选项时下拉先卸载、click 打不到
                  comboBlurTimer.current = setTimeout(() => setComboOpen(false), 150);
                }}
                onKeyDown={(e) => {
                  if (e.key === "Escape") setComboOpen(false);
                }}
                placeholder={t("如 4.0.201-941（可手填，不限于候选）")}
                spellCheck={false}
                style={inputStyle}
              />
              {selectedCandidate && !selectedCandidate.verified && (
                <div style={{ fontSize: 11, color: D.warn, marginTop: 6 }}>
                  ⚠ {t("这是 Release tag，未校验真实 build 号，可能符号化失败")}
                </div>
              )}
              {comboOpen && platform && filteredVersions.length > 0 && (
                <div
                  style={{
                    position: "absolute",
                    zIndex: 20,
                    left: 0,
                    right: 0,
                    top: "100%",
                    marginTop: 4,
                    maxHeight: 260,
                    overflowY: "auto",
                    background: D.surface,
                    border: `1px solid ${D.border}`,
                    borderRadius: 6,
                    boxShadow: "0 8px 24px rgba(0,0,0,0.18)",
                  }}
                >
                  {filteredVersions.map((v) => {
                    const badge = SOURCE_BADGE[v.source] || SOURCE_BADGE.release;
                    return (
                      <button
                        key={`${v.source}:${v.app_version}`}
                        type="button"
                        onMouseDown={(e) => e.preventDefault()}
                        onClick={() => {
                          versionTouched.current = true;
                          setAppVersion(v.app_version);
                          setComboOpen(false);
                        }}
                        title={
                          v.verified
                            ? `${v.tag || v.app_version} · ${(v.symbol_types || []).join(", ")}`
                            : t("这是 Release tag，未校验真实 build 号，可能符号化失败")
                        }
                        style={{
                          display: "flex",
                          width: "100%",
                          alignItems: "center",
                          justifyContent: "space-between",
                          gap: 8,
                          padding: "7px 10px",
                          border: "none",
                          borderBottom: `1px solid ${D.borderSm}`,
                          background: "transparent",
                          cursor: "pointer",
                          textAlign: "left",
                        }}
                      >
                        <span style={{ fontFamily: MONO, fontSize: 12, color: D.text1, wordBreak: "break-all" }}>
                          {v.verified ? "" : "⚠ "}
                          {v.app_version}
                        </span>
                        <span style={{ display: "inline-flex", alignItems: "center", gap: 6, flex: "0 0 auto" }}>
                          {v.asset_size > 0 && (
                            <span style={{ fontSize: 10, color: D.text3 }}>{humanBytes(v.asset_size)}</span>
                          )}
                          <span
                            style={{
                              fontSize: 10,
                              fontWeight: 700,
                              padding: "1px 6px",
                              borderRadius: 999,
                              color: badge.fg,
                              background: badge.bg,
                            }}
                          >
                            {t(badge.label)}
                          </span>
                        </span>
                      </button>
                    );
                  })}
                </div>
              )}
              {versionWarnings.length > 0 && (
                <ul style={{ margin: "8px 0 0", paddingLeft: 18, fontSize: 11, color: D.warn, lineHeight: 1.6 }}>
                  {versionWarnings.map((w, i) => (
                    <li key={i}>{w}</li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </div>

        {/* 5. preflight 三档状态条 */}
        {(preflightLoading || preflight || preflightError) && (
          <div style={cardStyle}>
            {preflightLoading && !preflight && (
              <div style={{ fontSize: 12, color: D.text2 }}>{t("符号可用性预检中…")}</div>
            )}
            {preflightError && (
              <div style={{ fontSize: 12, color: D.danger }}>
                {t("预检失败")}：{preflightError}
              </div>
            )}
            {preflight && (
              <div
                style={{
                  background: STATUS_STYLE[preflight.status].bg,
                  border: `1px solid ${STATUS_STYLE[preflight.status].fg}55`,
                  borderRadius: 6,
                  padding: 12,
                }}
              >
                <div style={{ fontSize: 13, fontWeight: 700, color: STATUS_STYLE[preflight.status].fg }}>
                  {STATUS_STYLE[preflight.status].icon}{" "}
                  {preflight.status === "cached"
                    ? t("符号包已就绪")
                    : preflight.status === "available"
                      ? t("符号包可下载")
                      : t("该版本无符号表")}
                </div>
                <div style={{ fontSize: 12, color: D.text2, marginTop: 4 }}>
                  {preflight.status === "missing"
                    ? t("该版本无符号表，符号化不会有任何效果")
                    : preflight.eta_hint}
                </div>
                {preflight.status === "missing" && preflight.eta_hint && (
                  <div style={{ fontSize: 12, color: D.text2, marginTop: 2 }}>{preflight.eta_hint}</div>
                )}
                {preflight.suggestions?.reason && (
                  <div style={{ fontSize: 12, color: D.text2, marginTop: 6 }}>{preflight.suggestions.reason}</div>
                )}
                {preflight.symbol_sources?.length > 0 && (
                  <ul style={{ margin: "8px 0 0", paddingLeft: 18, fontSize: 11, color: D.text2, lineHeight: 1.6 }}>
                    {preflight.symbol_sources.map((s, i) => (
                      <li key={i}>
                        <span style={{ fontFamily: MONO }}>{s.source}</span> — {s.detail}
                      </li>
                    ))}
                  </ul>
                )}
                {preflight.status === "missing" && (preflight.suggestions?.nearby_versions?.length ?? 0) > 0 && (
                  <div style={{ marginTop: 10 }}>
                    <div style={{ fontSize: 11, color: D.text3, marginBottom: 6 }}>
                      {t("邻近版本（点一下直接换掉版本号）")}
                    </div>
                    <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                      {(preflight.suggestions.nearby_versions || []).map((nv) => (
                        <button
                          key={nv}
                          type="button"
                          onClick={() => {
                            versionTouched.current = true;
                            setAppVersion(nv);
                          }}
                          style={{
                            ...btnBase,
                            fontFamily: MONO,
                            background: D.surface,
                            borderColor: `${D.accent}66`,
                            color: D.accent,
                          }}
                        >
                          {nv}
                        </button>
                      ))}
                    </div>
                  </div>
                )}
                {preflight.status === "missing" && (
                  <div style={{ marginTop: 10 }}>
                    <button type="button" onClick={jumpToUpload} style={{ ...btnBase, background: D.surface }}>
                      ⬆ {t("去上传符号包")}
                    </button>
                    {preflight.suggestions?.upload_hint && (
                      <div style={{ fontSize: 11, color: D.text3, marginTop: 6 }}>{preflight.suggestions.upload_hint}</div>
                    )}
                  </div>
                )}
                {preflight.warnings?.length > 0 && (
                  <ul style={{ margin: "8px 0 0", paddingLeft: 18, fontSize: 11, color: D.warn, lineHeight: 1.6 }}>
                    {preflight.warnings.map((w, i) => (
                      <li key={i}>{w}</li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </div>
        )}

        {/* 6. 提交 */}
        <div style={cardStyle}>
          <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
            <button
              type="button"
              onClick={onSubmit}
              disabled={running || !stack.trim()}
              style={{
                ...btnBase,
                padding: "8px 18px",
                fontSize: 13,
                border: "none",
                background: confirmMissing ? D.danger : D.accent,
                color: "#FFFFFF",
                opacity: running || !stack.trim() ? 0.6 : 1,
                cursor: running || !stack.trim() ? "not-allowed" : "pointer",
              }}
            >
              {running
                ? `⏳ ${t("符号化中…")} ${elapsed}s`
                : confirmMissing
                  ? t("确定仍要符号化？")
                  : `▶ ${t("开始符号化")}`}
            </button>
            {confirmMissing && !running && (
              <button type="button" onClick={() => setConfirmMissing(false)} style={btnBase}>
                {t("取消")}
              </button>
            )}
            {running && preflight?.status === "available" && (
              <span style={{ fontSize: 12, color: D.warn }}>
                {t("首次下载符号包，请勿关闭页面")}
              </span>
            )}
            {!running && preflight?.status === "missing" && !confirmMissing && (
              <span style={{ fontSize: 12, color: D.danger }}>{t("当前版本无符号表，提交需二次确认")}</span>
            )}
          </div>
          {runError && (
            <div style={{ marginTop: 10, fontSize: 12, color: D.danger }}>
              {t("符号化失败")}：{runError}
            </div>
          )}
        </div>

        {/* 7. 结果区 */}
        {result && (
          <>
            <div className="grid grid-cols-1 gap-3 lg:grid-cols-2" style={{ marginBottom: 16 }}>
              <StackPane title={t("原始堆栈")} text={stack} tone={D.text2} />
              <StackPane
                title={`${t("符号化后")}${result.changed ? "" : ` · ${t("未发生变化")}`}`}
                text={result.symbolicated_stack}
                tone={result.changed ? D.ok : D.warn}
              />
            </div>

            <div style={cardStyle}>
              <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 10 }}>{t("诊断面板")}</div>

              <div
                style={{
                  background: D.surfaceAlt,
                  border: `1px solid ${D.borderSm}`,
                  borderRadius: 6,
                  padding: 12,
                  marginBottom: 12,
                }}
              >
                <div style={{ fontSize: 12, fontWeight: 600, color: D.text1, fontFamily: MONO, wordBreak: "break-word" }}>
                  {frameStatsLine}
                </div>
                <div style={{ fontSize: 11, color: D.text3, marginTop: 6, lineHeight: 1.7 }}>
                  {t("系统库/三方库帧不解析是正常的——强行匹配会产生错误符号，比裸地址更误导")}
                </div>
                {result.frame_stats.unparsed_lines > 0 && (
                  <div style={{ fontSize: 11, color: D.text3, marginTop: 4 }}>
                    {t("无法按帧解析的行")}：{result.frame_stats.unparsed_lines}
                  </div>
                )}
                {!result.frame_stats.app_module && (
                  <div style={{ fontSize: 11, color: D.text3, marginTop: 4 }}>
                    {t("未能推断出 App module，因此只给粗粒度统计（不区分 App 帧 / 系统库帧）")}
                  </div>
                )}
              </div>

              <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
                <InsightField
                  label={t("堆栈质量")}
                  value={`${result.stack_quality_before} → ${result.stack_quality_after}`}
                  origin={result.changed ? t("已符号化") : t("堆栈未变化")}
                />
                <InsightField
                  label={t("符号 profile")}
                  value={result.symbol_profile || "—"}
                  origin={`${t("路由置信度")} ${result.routing_confidence || "—"}`}
                />
                <InsightField label={t("源码仓库")} value={result.github_repo || "—"} origin={t("实际使用")} />
                <InsightField
                  label={t("平台 / 版本")}
                  value={`${PLATFORM_LABEL[result.platform] || result.platform} · ${result.app_version || "—"}`}
                  origin={t("实际使用")}
                />
                <InsightField label={t("耗时")} value={`${(result.duration_ms / 1000).toFixed(1)}s`} origin={t("后端处理")} />
                <InsightField
                  label={t("可用符号包")}
                  value={String(result.available_symbol_packages?.length || 0)}
                  origin={t("已上传记录")}
                />
              </div>

              {/* warnings 有则显示，没有不显示空板块（feedback_report_only_anomalies） */}
              {result.warnings?.length > 0 && (
                <div
                  style={{
                    marginTop: 12,
                    background: D.warnBg,
                    border: `1px solid ${D.warn}44`,
                    borderRadius: 6,
                    padding: 12,
                  }}
                >
                  <div style={{ fontSize: 12, fontWeight: 600, color: D.warn, marginBottom: 6 }}>
                    ⚠ {t("告警")}（{result.warnings.length}）
                  </div>
                  <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12, color: D.text2, lineHeight: 1.7 }}>
                    {result.warnings.map((w, i) => (
                      <li key={i}>{w}</li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          </>
        )}

        {/* 8. 符号包上传框 */}
        <div
          ref={uploadRef}
          style={{
            ...cardStyle,
            border: `1px solid ${uploadFlash ? D.accent : D.border}`,
            boxShadow: uploadFlash ? `0 0 0 3px ${D.accentSoft}` : "none",
            transition: "box-shadow 0.25s, border-color 0.25s",
          }}
        >
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>⬆ {t("上传符号包")}</div>
          <div style={{ fontSize: 11, color: D.text3, marginBottom: 10 }}>
            {t("Jenkins 只在正式包上传符号表，灰度包通常需要手动补一份")}
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <div>
              <div style={{ fontSize: 11, color: D.text3, marginBottom: 6 }}>{t("平台")}</div>
              <select value={upPlatform} onChange={(e) => setUpPlatform(e.target.value)} style={{ ...inputStyle, fontFamily: "inherit" }}>
                <option value="ios">iOS</option>
                <option value="android">Android</option>
                <option value="flutter">Flutter</option>
              </select>
            </div>
            <div>
              <div style={{ fontSize: 11, color: D.text3, marginBottom: 6 }}>{t("版本号")}</div>
              <input
                value={upVersion}
                onChange={(e) => setUpVersion(e.target.value)}
                placeholder="4.0.201-941"
                spellCheck={false}
                style={inputStyle}
              />
            </div>
            <div>
              <div style={{ fontSize: 11, color: D.text3, marginBottom: 6 }}>{t("符号类型")}</div>
              <select value={upType} onChange={(e) => setUpType(e.target.value)} style={{ ...inputStyle, fontFamily: "inherit" }}>
                {SYMBOL_TYPES.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <div style={{ fontSize: 11, color: D.text3, marginBottom: 6 }}>{t("文件")}</div>
              <input
                type="file"
                onChange={(e) => setUpFile(e.target.files?.[0] || null)}
                style={{ ...inputStyle, fontFamily: "inherit", padding: "5px 8px" }}
              />
            </div>
          </div>
          <div style={{ display: "flex", gap: 12, alignItems: "center", marginTop: 12, flexWrap: "wrap" }}>
            <button
              type="button"
              onClick={onUpload}
              disabled={upBusy}
              style={{
                ...btnBase,
                border: "none",
                background: D.accent,
                color: "#FFFFFF",
                opacity: upBusy ? 0.6 : 1,
                cursor: upBusy ? "not-allowed" : "pointer",
              }}
            >
              {upBusy ? `⏳ ${t("上传中…")}` : t("上传")}
            </button>
            {appVersion.trim() && (
              <button
                type="button"
                onClick={() => {
                  setUpVersion(appVersion.trim());
                  if (platform) setUpPlatform(platform);
                }}
                style={btnBase}
              >
                {t("填入当前版本")}
              </button>
            )}
            {upMsg && <span style={{ fontSize: 12, color: D.ok }}>✓ {upMsg}</span>}
            {upErr && <span style={{ fontSize: 12, color: D.danger }}>✗ {upErr}</span>}
          </div>
          <div style={{ fontSize: 11, color: D.text3, marginTop: 8, lineHeight: 1.7 }}>
            {t("dsym / dart_symbols / native_symbols 必须是 zip 或 tar.gz 压缩包；proguard_mapping 传 mapping.txt 原文件")}
          </div>
        </div>
      </div>
    </div>
  );
}
