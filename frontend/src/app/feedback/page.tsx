"use client";

import { useState, useRef } from "react";

// For file uploads, post directly to backend (bypass Next.js proxy body size limit)
const BACKEND_URL = typeof window !== "undefined"
  ? (process.env.NEXT_PUBLIC_API_URL || `${window.location.protocol}//${window.location.hostname}:8000`)
  : "http://localhost:8000";

function Toast({ msg, type, onClose }: { msg: string; type: "success" | "error"; onClose: () => void }) {
  return (
    <div className={`fixed bottom-6 right-6 z-50 rounded-lg px-4 py-2.5 text-sm font-medium text-white shadow-lg ${type === "success" ? "bg-green-600" : "bg-red-600"}`}>
      {msg}
      <button onClick={onClose} className="ml-3 text-white/70 hover:text-white">✕</button>
    </div>
  );
}

export default function FeedbackPage() {
  const CATEGORIES = [
    "硬件交互（蓝牙连接，固件升级，文件传输，音频播放，音频剪辑、音质不佳等）",
    "文件首页（首页所有功能，列表显示，移动文件夹，批量转写，重命名，合并音频，删除文件，导入音频，时钟问题导致文件名不一致）",
    "文件管理（转写，总结，文件编辑，分享导出，更多菜单，ASK Plaud，PCS）",
    "用户系统与管理（账号登录注册，Onboarding，个人资料，偏好设置，app push 通知）",
    "商业化（会员购买，会员转化）",
    "其他通用模块（Autoflow，模版社区，Plaud WEB、集成、功能许愿池、推荐朋友、隐私与安全、帮助与支持等其他功能）",
    "iZYREC 硬件问题",
  ];

  const [form, setForm] = useState({
    description: "",
    category: "",
    device_sn: "",
    firmware: "",
    app_version: "",
    platform: "APP",
    priority: "L",
    zendesk: "",
  });
  const [files, setFiles] = useState<File[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [importing, setImporting] = useState(false);
  const [toast, setToast] = useState<{ msg: string; type: "success" | "error" } | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const importFromZendesk = async () => {
    const zd = form.zendesk.trim();
    if (!zd) { setToast({ msg: "请先输入 Zendesk 工单号", type: "error" }); setTimeout(() => setToast(null), 3000); return; }
    setImporting(true);
    try {
      const fd = new FormData();
      fd.append("zendesk_input", zd);
      const res = await fetch(`${BACKEND_URL}/api/feedback/import-zendesk`, { method: "POST", body: fd });
      if (!res.ok) { const t = await res.text(); throw new Error(t); }
      const data = await res.json();

      // Auto-fill form with AI summary (user can still edit)
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
      setToast({ msg: `已导入 Zendesk #${data.ticket_id}（${data.comment_count} 条聊天记录）`, type: "success" });
    } catch (e: any) {
      setToast({ msg: `导入失败: ${e.message}`, type: "error" });
    } finally {
      setImporting(false);
      setTimeout(() => setToast(null), 4000);
    }
  };

  const update = (key: string, val: string) => setForm((p) => ({ ...p, [key]: val }));

  const addFiles = (newFiles: FileList | null) => {
    if (!newFiles) return;
    const valid: File[] = [];
    for (const f of Array.from(newFiles)) {
      if (f.size > 50 * 1024 * 1024) {
        setToast({ msg: `${f.name} 超过 50MB 限制（${formatSize(f.size)}）`, type: "error" });
        setTimeout(() => setToast(null), 4000);
      } else {
        valid.push(f);
      }
    }
    if (valid.length) setFiles((prev) => [...prev, ...valid]);
  };

  const removeFile = (idx: number) => setFiles((prev) => prev.filter((_, i) => i !== idx));

  const formatSize = (bytes: number) => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  const MAX_FILE_SIZE = 50 * 1024 * 1024; // 50MB

  const submit = async () => {
    if (!form.description.trim()) {
      setToast({ msg: "请填写问题描述", type: "error" });
      setTimeout(() => setToast(null), 3000);
      return;
    }

    // Check file sizes
    const oversized = files.find((f) => f.size > MAX_FILE_SIZE);
    if (oversized) {
      setToast({ msg: `文件 ${oversized.name} 超过 50MB 限制（${formatSize(oversized.size)}），请压缩后重试`, type: "error" });
      setTimeout(() => setToast(null), 5000);
      return;
    }

    setSubmitting(true);
    try {
      const fd = new FormData();
      fd.append("description", form.description);
      fd.append("category", form.category);
      fd.append("device_sn", form.device_sn);
      fd.append("firmware", form.firmware);
      fd.append("app_version", form.app_version);
      fd.append("platform", form.platform);
      fd.append("priority", form.priority);
      fd.append("zendesk", form.zendesk);
      // Pass username
      const username = typeof window !== "undefined" ? localStorage.getItem("jarvis_username") || "" : "";
      fd.append("username", username);
      for (const f of files) {
        fd.append("log_files", f);
      }

      const res = await fetch(`${BACKEND_URL}/api/feedback`, { method: "POST", body: fd });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text);
      }
      const data = await res.json();

      // Redirect to in-progress tab immediately
      window.location.href = "/?tab=in_progress";
    } catch (e: any) {
      setToast({ msg: `提交失败: ${e.message}`, type: "error" });
    } finally {
      setSubmitting(false);
      setTimeout(() => setToast(null), 4000);
    }
  };

  return (
    <div className="min-h-full">
      <header className="sticky top-0 z-10 border-b border-gray-200 bg-white/80 backdrop-blur-sm">
        <div className="flex items-center justify-between px-6 py-3">
          <div>
            <h1 className="text-lg font-semibold">提交反馈</h1>
            <p className="text-xs text-gray-400">手动上传用户问题和日志文件</p>
          </div>
        </div>
      </header>

      <div className="mx-auto max-w-2xl px-6 py-6">
        <div className="space-y-5">

          {/* Problem description */}
          <div>
            <label className="mb-1.5 block text-sm font-medium text-gray-700">
              问题描述 <span className="text-red-500">*</span>
            </label>
            <textarea
              value={form.description}
              onChange={(e) => update("description", e.target.value)}
              placeholder="请详细描述用户遇到的问题..."
              rows={5}
              className="w-full rounded-lg border border-gray-200 px-3 py-2.5 text-sm text-gray-700 outline-none transition-colors focus:border-black focus:ring-1 focus:ring-black"
            />
          </div>

          {/* Problem category */}
          <div>
            <label className="mb-1.5 block text-sm font-medium text-gray-700">问题分类</label>
            <select
              value={form.category}
              onChange={(e) => update("category", e.target.value)}
              className="w-full rounded-lg border border-gray-200 px-3 py-2.5 text-sm text-gray-700 outline-none focus:border-black"
            >
              <option value="">请选择问题分类</option>
              {CATEGORIES.map((cat) => (
                <option key={cat} value={cat}>{cat}</option>
              ))}
            </select>
          </div>

          {/* Platform + Priority row */}
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="mb-1.5 block text-sm font-medium text-gray-700">平台</label>
              <select
                value={form.platform}
                onChange={(e) => update("platform", e.target.value)}
                className="w-full rounded-lg border border-gray-200 px-3 py-2.5 text-sm text-gray-700 outline-none focus:border-black"
              >
                <option value="APP">APP (移动端)</option>
                <option value="Web">Web (网页端)</option>
                <option value="Desktop">Desktop (桌面端)</option>
              </select>
            </div>
            <div>
              <label className="mb-1.5 block text-sm font-medium text-gray-700">优先级</label>
              <div className="flex gap-3 pt-1">
                {[
                  { value: "H", label: "高", color: "peer-checked:bg-red-50 peer-checked:text-red-700 peer-checked:ring-red-200" },
                  { value: "L", label: "低", color: "peer-checked:bg-gray-100 peer-checked:text-gray-700 peer-checked:ring-gray-300" },
                ].map((opt) => (
                  <label key={opt.value} className="flex-1 cursor-pointer">
                    <input type="radio" name="priority" value={opt.value} checked={form.priority === opt.value} onChange={(e) => update("priority", e.target.value)} className="peer sr-only" />
                    <div className={`rounded-lg border border-gray-200 py-2 text-center text-sm font-medium text-gray-500 ring-1 ring-transparent transition-all peer-checked:border-transparent ${opt.color}`}>
                      {opt.label}
                    </div>
                  </label>
                ))}
              </div>
            </div>
          </div>

          {/* Device SN + Firmware */}
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="mb-1.5 block text-sm font-medium text-gray-700">设备 SN</label>
              <input
                value={form.device_sn}
                onChange={(e) => update("device_sn", e.target.value)}
                placeholder="如 8801030171711129"
                className="w-full rounded-lg border border-gray-200 px-3 py-2.5 font-mono text-sm text-gray-700 outline-none focus:border-black"
              />
            </div>
            <div>
              <label className="mb-1.5 block text-sm font-medium text-gray-700">固件版本</label>
              <input
                value={form.firmware}
                onChange={(e) => update("firmware", e.target.value)}
                placeholder="如 2.1.0"
                className="w-full rounded-lg border border-gray-200 px-3 py-2.5 text-sm text-gray-700 outline-none focus:border-black"
              />
            </div>
          </div>

          {/* APP version + Zendesk */}
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="mb-1.5 block text-sm font-medium text-gray-700">APP 版本</label>
              <input
                value={form.app_version}
                onChange={(e) => update("app_version", e.target.value)}
                placeholder="如 3.5.2"
                className="w-full rounded-lg border border-gray-200 px-3 py-2.5 text-sm text-gray-700 outline-none focus:border-black"
              />
            </div>
            <div>
              <label className="mb-1.5 block text-sm font-medium text-gray-700">Zendesk 工单号</label>
              <div className="flex gap-2">
                <input
                  value={form.zendesk}
                  onChange={(e) => update("zendesk", e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); importFromZendesk(); } }}
                  placeholder="输入工单号，回车导入"
                  className="flex-1 rounded-lg border border-gray-200 px-3 py-2.5 text-sm text-gray-700 outline-none focus:border-black"
                />
                <button
                  type="button"
                  onClick={importFromZendesk}
                  disabled={importing || !form.zendesk.trim()}
                  className="flex-shrink-0 rounded-lg bg-blue-600 px-3 py-2 text-sm font-medium text-white transition-colors hover:bg-blue-700 disabled:opacity-40"
                >
                  {importing ? (
                    <span className="flex items-center gap-1.5">
                      <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/30 border-t-white" />
                      导入中
                    </span>
                  ) : "导入"}
                </button>
              </div>
              <p className="mt-1 text-[11px] text-gray-400">输入工单号后点击「导入」，AI 将自动总结聊天记录并填充表单</p>
            </div>
          </div>

          {/* Log file upload */}
          <div>
            <label className="mb-1.5 block text-sm font-medium text-gray-700">日志文件</label>
            <div
              onClick={() => fileRef.current?.click()}
              onDragOver={(e) => { e.preventDefault(); e.stopPropagation(); }}
              onDrop={(e) => { e.preventDefault(); e.stopPropagation(); addFiles(e.dataTransfer.files); }}
              className="flex cursor-pointer flex-col items-center justify-center rounded-lg border-2 border-dashed border-gray-200 bg-gray-50/50 px-6 py-8 transition-colors hover:border-gray-300 hover:bg-gray-50"
            >
              <svg className="mb-2 h-8 w-8 text-gray-300" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 16.5V9.75m0 0l3 3m-3-3l-3 3M6.75 19.5a4.5 4.5 0 01-1.41-8.775 5.25 5.25 0 0110.338-2.32 3.75 3.75 0 013.537 5.344A4.5 4.5 0 0118 19.5H6.75z" />
              </svg>
              <p className="text-sm text-gray-500">点击或拖拽上传日志文件</p>
              <p className="mt-0.5 text-xs text-gray-400">支持 .plaud, .log, .zip, .gz 格式（单个文件 ≤ 50MB）</p>
              <input
                ref={fileRef}
                type="file"
                multiple
                accept=".plaud,.log,.zip,.gz,.txt,.rtf"
                onChange={(e) => addFiles(e.target.files)}
                className="hidden"
              />
            </div>

            {/* File list */}
            {files.length > 0 && (
              <div className="mt-3 space-y-1.5">
                {files.map((f, i) => (
                  <div key={i} className="flex items-center justify-between rounded-lg bg-gray-50 px-3 py-2">
                    <div className="flex items-center gap-2 min-w-0">
                      <svg className="h-4 w-4 flex-shrink-0 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" />
                      </svg>
                      <span className="truncate text-sm text-gray-700">{f.name}</span>
                      <span className="flex-shrink-0 text-xs text-gray-400">{formatSize(f.size)}</span>
                    </div>
                    <button onClick={() => removeFile(i)} className="ml-2 flex-shrink-0 rounded p-1 text-gray-400 hover:bg-gray-200 hover:text-gray-600">
                      <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" /></svg>
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Submit */}
          <button
            onClick={submit}
            disabled={submitting}
            className="w-full rounded-lg bg-black py-3 text-sm font-semibold text-white transition-colors hover:bg-gray-800 disabled:opacity-50"
          >
            {submitting ? (
              <span className="flex items-center justify-center gap-2">
                <span className="h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white" />
                提交中...
              </span>
            ) : (
              "提交反馈"
            )}
          </button>

          <p className="text-center text-xs text-gray-400">
            提交后可在「待处理」列表中查看，点击「分析」触发 AI 分析
          </p>
        </div>
      </div>

      {toast && <Toast msg={toast.msg} type={toast.type} onClose={() => setToast(null)} />}
    </div>
  );
}
