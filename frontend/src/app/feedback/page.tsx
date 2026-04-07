"use client";

import { useT, useLang } from "@/lib/i18n";
import { useState, useRef } from "react";
import { Toast } from "@/components/Toast";

function getBackendUrl(): string {
  if (typeof window === "undefined") return "http://localhost:8000";
  const explicit = process.env.NEXT_PUBLIC_BACKEND_URL;
  if (explicit && !explicit.includes("backend:")) return explicit;
  return `${window.location.protocol}//${window.location.hostname}:8000`;
}
const BACKEND_URL = getBackendUrl();

const S = {
  surface: "#F8F9FA", overlay: "#FFFFFF", hover: "#EEF0F2",
  border: "rgba(0,0,0,0.08)", accent: "#B8922E",
  text1: "#111827", text2: "#6B7280", text3: "#9CA3AF",
};

const inputCls = "w-full rounded-lg px-3 py-2.5 text-sm font-sans outline-none transition-colors";
const inputStyle = { background: S.overlay, border: `1px solid ${S.border}`, color: S.text1 };
const labelCls = "mb-1.5 block text-sm font-medium";


export default function FeedbackPage() {
  const CATEGORIES_DATA: { value: string; cn: string; en: string }[] = [
    { value: "hardware", cn: "硬件交互（蓝牙连接，固件升级，文件传输，音频播放，音频剪辑、音质不佳等）", en: "Hardware (Bluetooth, firmware, file transfer, audio playback, clipping, sound quality)" },
    { value: "file_home", cn: "文件首页（首页所有功能，列表显示，移动文件夹，批量转写，重命名，合并音频，删除文件，导入音频，时钟问题导致文件名不一致）", en: "File Home (listing, folders, batch transcription, rename, merge, delete, import, clock issues)" },
    { value: "file_mgmt", cn: "文件管理（转写，总结，文件编辑，分享导出，更多菜单，ASK Plaud，PCS）", en: "File Management (transcription, summary, edit, share/export, ASK Plaud, PCS)" },
    { value: "user_system", cn: "用户系统与管理（账号登录注册，Onboarding，个人资料，偏好设置，app push 通知）", en: "User System (login, onboarding, profile, preferences, push notifications)" },
    { value: "monetization", cn: "商业化（会员购买，会员转化）", en: "Monetization (membership purchase, conversion)" },
    { value: "other", cn: "其他通用模块（Autoflow，模版社区，Plaud WEB、集成、功能许愿池、推荐朋友、隐私与安全、帮助与支持等其他功能）", en: "Other (Autoflow, templates, Plaud Web, integrations, wishlist, referral, privacy, help)" },
    { value: "izyrec", cn: "iZYREC 硬件问题", en: "iZYREC Hardware Issues" },
  ];

  const [form, setForm] = useState({
    description: "", category: "", device_sn: "", firmware: "",
    app_version: "", platform: "APP", priority: "L", zendesk: "", occurred_at: "",
  });
  const t = useT();
  const currentLang = useLang();
  const [files, setFiles] = useState<File[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [importing, setImporting] = useState(false);
  const [toast, setToast] = useState<{ msg: string; type: "success" | "error" } | null>(null);
  const [showNoLogConfirm, setShowNoLogConfirm] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const [isDragOver, setIsDragOver] = useState(false);

  const importFromZendesk = async () => {
    const zd = form.zendesk.trim();
    if (!zd) { setToast({ msg: t("请先输入 Zendesk 工单号"), type: "error" }); setTimeout(() => setToast(null), 3000); return; }
    setImporting(true);
    try {
      const fd = new FormData();
      fd.append("zendesk_input", zd);
      const res = await fetch(`${BACKEND_URL}/api/feedback/import-zendesk`, { method: "POST", body: fd });
      if (!res.ok) {
        const errText = await res.text();
        if (res.status === 503 || errText.includes("ZENDESK_NOT_CONFIGURED")) throw new Error("ZENDESK_NOT_CONFIGURED");
        throw new Error(errText);
      }
      const data = await res.json();
      setForm((prev) => ({
        ...prev,
        description: data.description || prev.description,
        category: data.category || prev.category,
        priority: data.priority || prev.priority,
        device_sn: data.device_sn || prev.device_sn,
        firmware: data.firmware || prev.firmware,
        app_version: data.app_version || prev.app_version,
        zendesk: data.zendesk_url || prev.zendesk,
      }));
      setToast({ msg: `${t("已导入 Zendesk")} #${data.ticket_id}（${data.comment_count} ${t("条聊天记录")}）`, type: "success" });
    } catch (e: any) {
      if (e.message === "ZENDESK_NOT_CONFIGURED") {
        setToast({ msg: t("Zendesk 导入功能暂未配置，请联系管理员设置 Zendesk API 凭证"), type: "error" });
      } else {
        setToast({ msg: `${t("导入失败")}: ${e.message}`, type: "error" });
      }
    } finally { setImporting(false); setTimeout(() => setToast(null), 5000); }
  };

  const update = (key: string, val: string) => setForm((p) => ({ ...p, [key]: val }));

  const formatSize = (bytes: number) => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  const addFiles = (newFiles: FileList | null) => {
    if (!newFiles) return;
    const valid: File[] = [];
    for (const f of Array.from(newFiles)) {
      if (f.size > 50 * 1024 * 1024) {
        setToast({ msg: `${f.name} ${t("超过 50MB 限制")}（${formatSize(f.size)}）`, type: "error" });
        setTimeout(() => setToast(null), 4000);
      } else { valid.push(f); }
    }
    if (valid.length) setFiles((prev) => [...prev, ...valid]);
  };

  const removeFile = (idx: number) => setFiles((prev) => prev.filter((_, i) => i !== idx));

  const submit = async (skipLogCheck = false) => {
    if (!form.description.trim()) {
      setToast({ msg: t("请填写问题描述"), type: "error" }); setTimeout(() => setToast(null), 3000); return;
    }
    const oversized = files.find((f) => f.size > 50 * 1024 * 1024);
    if (oversized) {
      setToast({ msg: `${t("文件")} ${oversized.name} ${t("超过 50MB 限制")}`, type: "error" }); setTimeout(() => setToast(null), 5000); return;
    }
    if (!skipLogCheck && files.length === 0) { setShowNoLogConfirm(true); return; }
    setSubmitting(true); setUploadProgress(0);
    try {
      const fd = new FormData();
      fd.append("description", form.description); fd.append("category", form.category);
      fd.append("device_sn", form.device_sn); fd.append("firmware", form.firmware);
      fd.append("app_version", form.app_version); fd.append("platform", form.platform);
      fd.append("priority", form.priority); fd.append("zendesk", form.zendesk);
      if (form.occurred_at) fd.append("occurred_at", form.occurred_at);
      const username = typeof window !== "undefined" ? localStorage.getItem("appllo_username") || "" : "";
      fd.append("username", username);
      for (const f of files) fd.append("log_files", f);
      await new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", `${BACKEND_URL}/api/feedback`);
        xhr.timeout = 120000;
        xhr.upload.onprogress = (e) => { if (e.lengthComputable) setUploadProgress(Math.round((e.loaded / e.total) * 100)); };
        xhr.onload = () => { if (xhr.status >= 200 && xhr.status < 300) { try { resolve(JSON.parse(xhr.responseText)); } catch { resolve({}); } } else { reject(new Error(xhr.responseText || `HTTP ${xhr.status}`)); } };
        xhr.onerror = () => reject(new Error(t("网络错误，请检查网络连接")));
        xhr.ontimeout = () => reject(new Error(t("上传超时（2分钟），请检查文件大小和网络")));
        xhr.send(fd);
      });
      window.location.href = "/?tab=in_progress";
    } catch (e: any) {
      setToast({ msg: `${t("提交失败")}: ${e.message}`, type: "error" }); setTimeout(() => setToast(null), 6000);
    } finally { setSubmitting(false); setUploadProgress(0); }
  };

  return (
    <div className="min-h-full">
      {/* Header */}
      <header className="sticky top-0 z-10 backdrop-blur-md"
        style={{ background: "rgba(255,255,255,0.92)", borderBottom: `1px solid ${S.border}` }}>
        <div className="px-6 py-3">
          <h1 className="text-base font-semibold" style={{ color: S.text1 }}>{t("提交反馈")}</h1>
          <p className="text-xs mt-0.5" style={{ color: S.text3 }}>{t("手动上传用户问题和日志文件")}</p>
        </div>
      </header>

      <div className="mx-auto max-w-2xl px-6 py-6">
        <div className="space-y-5">

          {/* Description */}
          <div>
            <label className={labelCls} style={{ color: S.text2 }}>
              {t("问题描述")} <span style={{ color: "#DC2626" }}>*</span>
            </label>
            <textarea value={form.description} onChange={(e) => update("description", e.target.value)}
              placeholder={t("请详细描述用户遇到的问题...")} rows={5}
              className={inputCls} style={inputStyle} />
          </div>

          {/* Category */}
          <div>
            <label className={labelCls} style={{ color: S.text2 }}>{t("问题分类")}</label>
            <select value={form.category} onChange={(e) => update("category", e.target.value)}
              className={inputCls} style={inputStyle}>
              <option value="">{t("请选择问题分类")}</option>
              {CATEGORIES_DATA.map((cat) => (
                <option key={cat.value} value={cat.cn}>{currentLang === "en" ? cat.en : cat.cn}</option>
              ))}
            </select>
          </div>

          {/* Occurred at */}
          <div>
            <label className={labelCls} style={{ color: S.text2 }}>{t("问题发生时间")}</label>
            <input type="datetime-local" value={form.occurred_at}
              onChange={(e) => update("occurred_at", e.target.value)}
              className={inputCls} style={inputStyle} />
          </div>

          {/* Platform + Priority */}
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className={labelCls} style={{ color: S.text2 }}>{t("平台")}</label>
              <select value={form.platform} onChange={(e) => update("platform", e.target.value)}
                className={inputCls} style={inputStyle}>
                <option value="APP">APP</option>
                <option value="Web" disabled>Web</option>
                <option value="Desktop" disabled>Desktop</option>
              </select>
            </div>
            <div>
              <label className={labelCls} style={{ color: S.text2 }}>{t("优先级")}</label>
              <div className="flex gap-3 pt-1">
                {[
                  { value: "H", label: t("高"), activeBg: "rgba(239,68,68,0.15)", activeColor: "#DC2626", activeBorder: "rgba(239,68,68,0.3)" },
                  { value: "L", label: t("低"), activeBg: "rgba(0,0,0,0.04)", activeColor: S.text2, activeBorder: S.border },
                ].map((opt) => (
                  <label key={opt.value} className="flex-1 cursor-pointer">
                    <input type="radio" name="priority" value={opt.value} checked={form.priority === opt.value}
                      onChange={(e) => update("priority", e.target.value)} className="sr-only" />
                    <div className="rounded-lg py-2 text-center text-sm font-medium transition-all"
                      style={form.priority === opt.value
                        ? { background: opt.activeBg, color: opt.activeColor, border: `1px solid ${opt.activeBorder}` }
                        : { background: "transparent", color: S.text3, border: `1px solid ${S.border}` }}>
                      {opt.label}
                    </div>
                  </label>
                ))}
              </div>
            </div>
          </div>

          {/* SN + Firmware */}
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className={labelCls} style={{ color: S.text2 }}>{t("设备 SN")}</label>
              <input value={form.device_sn} onChange={(e) => update("device_sn", e.target.value)}
                placeholder={currentLang === "en" ? "e.g. 8801030171711129" : "如 8801030171711129"}
                className={inputCls + " font-mono"} style={inputStyle} />
            </div>
            <div>
              <label className={labelCls} style={{ color: S.text2 }}>{t("固件版本")}</label>
              <input value={form.firmware} onChange={(e) => update("firmware", e.target.value)}
                placeholder={currentLang === "en" ? "e.g. 2.1.0" : "如 2.1.0"}
                className={inputCls} style={inputStyle} />
            </div>
          </div>

          {/* APP version + Zendesk */}
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className={labelCls} style={{ color: S.text2 }}>{t("APP 版本")}</label>
              <input value={form.app_version} onChange={(e) => update("app_version", e.target.value)}
                placeholder={currentLang === "en" ? "e.g. 3.5.2" : "如 3.5.2"}
                className={inputCls} style={inputStyle} />
            </div>
            <div>
              <label className={labelCls} style={{ color: S.text2 }}>{t("Zendesk 工单号")}</label>
              <div className="flex gap-2">
                <input value={form.zendesk} onChange={(e) => update("zendesk", e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); importFromZendesk(); } }}
                  placeholder={t("输入工单号，回车导入")}
                  className={`flex-1 rounded-lg px-3 py-2.5 text-sm font-sans outline-none`}
                  style={inputStyle} />
                <button type="button" onClick={importFromZendesk} disabled={importing || !form.zendesk.trim()}
                  className="flex-shrink-0 rounded-lg px-3 py-2 text-sm font-medium transition-colors disabled:opacity-40"
                  style={{ background: "rgba(96,165,250,0.15)", color: "#2563EB", border: "1px solid rgba(96,165,250,0.25)" }}>
                  {importing ? (
                    <span className="flex items-center gap-1.5">
                      <span className="h-3.5 w-3.5 animate-spin rounded-full border-2" style={{ borderColor: "rgba(96,165,250,0.3)", borderTopColor: "#2563EB" }} />
                      {t("导入中")}
                    </span>
                  ) : t("导入")}
                </button>
              </div>
              <p className="mt-1 text-[11px]" style={{ color: S.text3 }}>
                {t("输入工单号后点击导入，AI 将自动总结聊天记录并填充表单")}
              </p>
            </div>
          </div>

          {/* File upload */}
          <div>
            <label className={labelCls} style={{ color: S.text2 }}>{t("日志文件")}</label>
            <div
              onClick={() => fileRef.current?.click()}
              onDragOver={(e) => { e.preventDefault(); e.stopPropagation(); setIsDragOver(true); }}
              onDragLeave={() => setIsDragOver(false)}
              onDrop={(e) => { e.preventDefault(); e.stopPropagation(); setIsDragOver(false); addFiles(e.dataTransfer.files); }}
              className="flex cursor-pointer flex-col items-center justify-center rounded-xl px-6 py-10 transition-all"
              style={{
                border: `2px dashed ${isDragOver ? S.accent : "rgba(0,0,0,0.10)"}`,
                background: isDragOver ? "rgba(184,146,46,0.05)" : "rgba(0,0,0,0.02)",
              }}>
              <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-xl"
                style={{ background: "rgba(0,0,0,0.03)", border: `1px solid ${S.border}` }}>
                <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5} style={{ color: S.text3 }}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 16.5V9.75m0 0l3 3m-3-3l-3 3M6.75 19.5a4.5 4.5 0 01-1.41-8.775 5.25 5.25 0 0110.338-2.32 3.75 3.75 0 013.537 5.344A4.5 4.5 0 0118 19.5H6.75z" />
                </svg>
              </div>
              <p className="text-sm font-medium" style={{ color: S.text2 }}>{t("点击或拖拽上传文件")}</p>
              <p className="mt-1 text-xs" style={{ color: S.text3 }}>
                {t("支持日志 (.plaud, .log, .zip, .gz) 和图片 (.png, .jpg, .gif)（≤ 50MB）")}
              </p>
              <input ref={fileRef} type="file" multiple
                accept=".plaud,.log,.zip,.gz,.txt,.rtf,.png,.jpg,.jpeg,.gif,.webp,.bmp"
                onChange={(e) => addFiles(e.target.files)} className="hidden" />
            </div>

            {files.length > 0 && (
              <div className="mt-3 space-y-1.5">
                {files.map((f, i) => {
                  const isImg = /\.(png|jpe?g|gif|webp|bmp)$/i.test(f.name);
                  return (
                    <div key={i} className="flex items-center justify-between rounded-lg px-3 py-2"
                      style={{ background: S.overlay, border: `1px solid ${S.border}` }}>
                      <div className="flex items-center gap-2 min-w-0">
                        {isImg ? (
                          <img src={URL.createObjectURL(f)} alt="" className="h-8 w-8 flex-shrink-0 rounded object-cover" />
                        ) : (
                          <svg className="h-4 w-4 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5} style={{ color: S.text3 }}>
                            <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" />
                          </svg>
                        )}
                        <span className="truncate text-sm" style={{ color: S.text1 }}>{f.name}</span>
                        <span className="flex-shrink-0 text-xs font-mono" style={{ color: S.text3 }}>{formatSize(f.size)}</span>
                      </div>
                      <button onClick={() => removeFile(i)} className="ml-2 flex-shrink-0 rounded-md p-1 transition-colors"
                        style={{ color: S.text3 }}>
                        <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                          <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                        </svg>
                      </button>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          {/* Submit */}
          <button onClick={() => submit()} disabled={submitting}
            className="w-full rounded-xl py-3 text-sm font-semibold transition-colors disabled:opacity-50"
            style={{ background: S.accent, color: "#0A0B0E" }}>
            {submitting ? (
              <span className="flex flex-col items-center gap-1">
                <span className="flex items-center gap-2">
                  <span className="h-4 w-4 animate-spin rounded-full border-2" style={{ borderColor: "rgba(10,11,14,0.3)", borderTopColor: "#0A0B0E" }} />
                  {uploadProgress < 100 ? `${t("上传中")} ${uploadProgress}%` : t("提交中...")}
                </span>
                {uploadProgress > 0 && uploadProgress < 100 && (
                  <span className="h-1 w-32 overflow-hidden rounded-full" style={{ background: "rgba(10,11,14,0.2)" }}>
                    <span className="block h-full rounded-full transition-all duration-300" style={{ width: `${uploadProgress}%`, background: "#0A0B0E" }} />
                  </span>
                )}
              </span>
            ) : t("提交反馈")}
          </button>

          <p className="text-center text-xs" style={{ color: S.text3 }}>
            {t("提交后工单将自动进入 AI 分析")}
          </p>
        </div>
      </div>

      {/* No log confirm dialog */}
      {showNoLogConfirm && (
        <div className="fixed inset-0 z-[70] flex items-center justify-center" style={{ background: "rgba(0,0,0,0.75)" }}>
          <div className="w-full max-w-md rounded-2xl p-6" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
            <div className="mb-4 flex items-start gap-3">
              <div className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-xl"
                style={{ background: "rgba(184,146,46,0.15)", border: "1px solid rgba(184,146,46,0.3)" }}>
                <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2} style={{ color: S.accent }}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" />
                </svg>
              </div>
              <div>
                <h3 className="text-base font-semibold" style={{ color: S.text1 }}>{t("未上传日志文件")}</h3>
                <p className="mt-1.5 text-sm" style={{ color: S.text2 }}>
                  {t("没有日志文件，AI 将无法分析用户的操作行为和设备状态，只能结合代码和产品知识回答问题。")}
                </p>
                <p className="mt-2 text-sm" style={{ color: S.text2 }}>
                  {t("适用于产品功能咨询、设计逻辑确认等场景。")}
                </p>
              </div>
            </div>
            <div className="flex justify-end gap-2">
              <button onClick={() => setShowNoLogConfirm(false)}
                className="rounded-lg px-4 py-2 text-sm font-medium transition-colors"
                style={{ border: `1px solid ${S.border}`, color: S.text2 }}>
                {t("返回上传日志")}
              </button>
              <button onClick={() => { setShowNoLogConfirm(false); submit(true); }}
                className="rounded-lg px-4 py-2 text-sm font-semibold"
                style={{ background: S.accent, color: "#0A0B0E" }}>
                {t("继续提交")}
              </button>
            </div>
          </div>
        </div>
      )}

      {toast && <Toast msg={toast.msg} type={toast.type} onClose={() => setToast(null)} />}
    </div>
  );
}
