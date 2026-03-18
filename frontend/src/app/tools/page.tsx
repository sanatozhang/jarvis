"use client";

import { useState, useRef, useCallback, DragEvent } from "react";
import { analyzeLostFile, LostFileFinderResult } from "@/lib/api";
import { useT, useLang } from "@/lib/i18n";

const S = {
  surface: "#F8F9FA",
  overlay: "#FFFFFF",
  hover: "#EEF0F2",
  border: "rgba(0,0,0,0.08)",
  accent: "#B8922E",
  accentBg: "rgba(184,146,46,0.06)",
  text1: "#111827",
  text2: "#6B7280",
  text3: "#9CA3AF",
};

const TIMEZONES = [
  { label: "CST 中国 (UTC+8)", value: 8 },
  { label: "JST 日本 (UTC+9)", value: 9 },
  { label: "EST 纽约 (UTC-5)", value: -5 },
  { label: "PST 洛杉矶 (UTC-8)", value: -8 },
  { label: "UTC (UTC+0)", value: 0 },
  { label: "CET 中欧 (UTC+1)", value: 1 },
  { label: "SGT 新加坡 (UTC+8)", value: 8 },
];

function today() {
  return new Date().toISOString().slice(0, 10);
}

function offsetDate(days: number) {
  const d = new Date();
  d.setDate(d.getDate() + days);
  return d.toISOString().slice(0, 10);
}

export default function ToolsPage() {
  const t = useT();
  const lang = useLang();

  // Lost File Finder state
  const [file, setFile] = useState<File | null>(null);
  const [date, setDate] = useState(today());
  const [tzOffset, setTzOffset] = useState(8);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<LostFileFinderResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const handleFile = useCallback((f: File) => {
    setFile(f);
    setResult(null);
    setError(null);
  }, []);

  const onDrop = useCallback(
    (e: DragEvent<HTMLDivElement>) => {
      e.preventDefault();
      setDragging(false);
      const f = e.dataTransfer.files[0];
      if (f) handleFile(f);
    },
    [handleFile],
  );

  const onDragOver = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragging(true);
  };

  const onDragLeave = () => setDragging(false);

  const handleAnalyze = async () => {
    if (!file) {
      setError(t("请先上传日志文件"));
      return;
    }
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const res = await analyzeLostFile(file, date, tzOffset);
      setResult(res);
    } catch (e: any) {
      setError(e.message || "分析失败");
    } finally {
      setLoading(false);
    }
  };

  const handleCopy = () => {
    if (!result) return;
    navigator.clipboard.writeText(result.markdown).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  };

  return (
    <div className="flex h-full flex-col overflow-hidden" style={{ background: S.surface }}>
      {/* Header */}
      <div
        className="flex h-[52px] flex-shrink-0 items-center justify-between px-6"
        style={{ background: S.overlay, borderBottom: `1px solid ${S.border}` }}
      >
        <div className="flex items-center gap-2">
          <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke={S.accent} strokeWidth={1.8}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M11 4a2 2 0 114 0v1a1 1 0 001 1h3a1 1 0 011 1v3a1 1 0 01-1 1h-1a2 2 0 100 4h1a1 1 0 011 1v3a1 1 0 01-1 1h-3a1 1 0 01-1-1v-1a2 2 0 10-4 0v1a1 1 0 01-1 1H7a1 1 0 01-1-1v-3a1 1 0 00-1-1H4a2 2 0 110-4h1a1 1 0 001-1V7a1 1 0 011-1h3a1 1 0 001-1V4z" />
          </svg>
          <h1 className="text-sm font-semibold" style={{ color: S.text1 }}>
            {t("常用工具")}
          </h1>
        </div>
      </div>

      {/* Content */}
      <div className="flex flex-1 gap-4 overflow-hidden p-4">
        {/* Tool list (left column) */}
        <div className="w-52 flex-shrink-0 space-y-1.5">
          <p className="px-2 text-[10px] font-semibold uppercase tracking-wider" style={{ color: S.text3 }}>
            {lang === "cn" ? "工具列表" : "Tool List"}
          </p>
          <div
            className="flex items-center gap-2.5 rounded-lg px-3 py-2.5 text-sm font-medium"
            style={{
              background: S.accentBg,
              color: S.text1,
              border: `1px solid rgba(184,146,46,0.18)`,
              cursor: "default",
            }}
          >
            <svg className="h-4 w-4 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke={S.accent} strokeWidth={1.8}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
            </svg>
            <span>{t("录音丢失排查")}</span>
          </div>
        </div>

        {/* Main panel */}
        <div className="flex min-w-0 flex-1 flex-col gap-4 overflow-y-auto">
          {/* Tool card */}
          <div className="rounded-xl p-5 space-y-4" style={{ background: S.overlay, border: `1px solid ${S.border}` }}>
            <div>
              <h2 className="text-base font-semibold" style={{ color: S.text1 }}>
                {t("录音丢失排查助手")}
              </h2>
              <p className="mt-0.5 text-xs" style={{ color: S.text2 }}>
                {lang === "cn"
                  ? "上传设备日志文件，按时间定位可能丢失的录音，生成排查报告。"
                  : "Upload a device log file to find potentially lost recordings by timestamp and generate a diagnostic report."}
              </p>
            </div>

            {/* Upload zone */}
            <div
              onDrop={onDrop}
              onDragOver={onDragOver}
              onDragLeave={onDragLeave}
              onClick={() => inputRef.current?.click()}
              className="flex flex-col items-center justify-center rounded-lg border-2 border-dashed transition-all cursor-pointer py-8 gap-2"
              style={{
                borderColor: dragging ? S.accent : "rgba(0,0,0,0.14)",
                background: dragging ? S.accentBg : S.surface,
              }}
            >
              <svg className="h-8 w-8" fill="none" viewBox="0 0 24 24" stroke={S.text3} strokeWidth={1.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5m-13.5-9L12 3m0 0l4.5 4.5M12 3v13.5" />
              </svg>
              {file ? (
                <div className="text-center">
                  <p className="text-sm font-medium" style={{ color: S.text1 }}>{file.name}</p>
                  <p className="text-xs" style={{ color: S.text2 }}>
                    {(file.size / 1024).toFixed(1)} KB — {lang === "cn" ? "点击更换" : "click to change"}
                  </p>
                </div>
              ) : (
                <div className="text-center">
                  <p className="text-sm" style={{ color: S.text2 }}>{t("拖拽文件到此处，或点击选择")}</p>
                  <p className="text-xs" style={{ color: S.text3 }}>
                    {lang === "cn" ? "支持 .plaud、.log 格式" : "Supports .plaud and .log files"}
                  </p>
                </div>
              )}
              <input
                ref={inputRef}
                type="file"
                accept=".plaud,.log"
                className="hidden"
                onChange={(e) => e.target.files?.[0] && handleFile(e.target.files[0])}
              />
            </div>

            {/* Date + Timezone row */}
            <div className="flex flex-wrap gap-4">
              <div className="flex-1 min-w-[160px]">
                <label className="mb-1 block text-xs font-medium" style={{ color: S.text2 }}>
                  {t("排查起始日期")}
                </label>
                <div className="flex items-center gap-1.5 flex-wrap">
                  <input
                    type="date"
                    value={date}
                    onChange={(e) => setDate(e.target.value)}
                    className="rounded-lg px-3 py-1.5 text-sm"
                    style={{
                      background: S.surface,
                      border: `1px solid ${S.border}`,
                      color: S.text1,
                      outline: "none",
                    }}
                  />
                  {/* Quick pick */}
                  {[
                    { label: lang === "cn" ? "今天" : "Today", offset: 0 },
                    { label: lang === "cn" ? "昨天" : "Yesterday", offset: -1 },
                    { label: lang === "cn" ? "前天" : "2d ago", offset: -2 },
                    { label: lang === "cn" ? "一周前" : "1w ago", offset: -7 },
                  ].map(({ label, offset }) => (
                    <button
                      key={offset}
                      onClick={() => setDate(offsetDate(offset))}
                      className="rounded-md px-2 py-1 text-xs font-medium transition-colors"
                      style={{
                        background: date === offsetDate(offset) ? S.accentBg : S.surface,
                        color: date === offsetDate(offset) ? S.accent : S.text2,
                        border: `1px solid ${date === offsetDate(offset) ? "rgba(184,146,46,0.3)" : S.border}`,
                      }}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </div>

              <div>
                <label className="mb-1 block text-xs font-medium" style={{ color: S.text2 }}>
                  {t("时区")}
                </label>
                <select
                  value={tzOffset}
                  onChange={(e) => setTzOffset(Number(e.target.value))}
                  className="rounded-lg px-3 py-1.5 text-sm"
                  style={{
                    background: S.surface,
                    border: `1px solid ${S.border}`,
                    color: S.text1,
                    outline: "none",
                    minWidth: 180,
                  }}
                >
                  {TIMEZONES.map((tz) => (
                    <option key={`${tz.label}-${tz.value}`} value={tz.value}>
                      {tz.label}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            {/* Analyze button */}
            <button
              onClick={handleAnalyze}
              disabled={loading}
              className="rounded-lg px-5 py-2.5 text-sm font-semibold transition-opacity"
              style={{
                background: S.accent,
                color: "#0A0B0E",
                opacity: loading ? 0.6 : 1,
                cursor: loading ? "not-allowed" : "pointer",
              }}
            >
              {loading ? (
                <span className="flex items-center gap-2">
                  <svg className="h-4 w-4 animate-spin" fill="none" viewBox="0 0 24 24">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                  </svg>
                  {t("排查中...")}
                </span>
              ) : (
                t("开始排查")
              )}
            </button>

            {/* Error */}
            {error && (
              <div
                className="rounded-lg px-4 py-3 text-sm"
                style={{ background: "rgba(239,68,68,0.08)", color: "#DC2626", border: "1px solid rgba(239,68,68,0.2)" }}
              >
                {error}
              </div>
            )}
          </div>

          {/* Result */}
          {result && (
            <div className="rounded-xl p-5 space-y-3" style={{ background: S.overlay, border: `1px solid ${S.border}` }}>
              {/* Result header */}
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <h3 className="text-sm font-semibold" style={{ color: S.text1 }}>
                    {t("分析结果")}
                  </h3>
                  <span className="rounded-full px-2.5 py-0.5 text-xs font-medium" style={{ background: "rgba(34,197,94,0.12)", color: "#16A34A" }}>
                    {t("共找到")} {result.total_records} {t("条同步记录")}
                    {result.anomaly_count > 0 && (
                      <>，{t("其中")} <span className="font-semibold">{result.anomaly_count}</span> {t("条时间戳异常")}</>
                    )}
                  </span>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    onClick={handleCopy}
                    className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors"
                    style={{ background: S.accentBg, color: S.accent, border: `1px solid rgba(184,146,46,0.2)` }}
                  >
                    <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                    </svg>
                    {copied ? t("已复制") : t("复制结果")}
                  </button>
                  <button
                    onClick={() => setResult(null)}
                    className="rounded-lg px-3 py-1.5 text-xs font-medium"
                    style={{ background: S.surface, color: S.text2, border: `1px solid ${S.border}` }}
                  >
                    {t("清除结果")}
                  </button>
                </div>
              </div>

              {/* Markdown content */}
              <pre
                className="overflow-auto rounded-lg p-4 text-xs leading-relaxed whitespace-pre-wrap font-mono"
                style={{ background: S.surface, color: S.text1, border: `1px solid ${S.border}`, maxHeight: 520 }}
              >
                {result.markdown}
              </pre>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
