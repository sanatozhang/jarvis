"use client";

import { useState, useCallback, useEffect } from "react";
import html2canvas from "html2canvas";
import { useT } from "@/lib/i18n";
import { useCurrentUser } from "@/components/AuthProvider";
import { submitSiteFeedback } from "@/lib/api";

const GOLD = "#B8922E";

export default function FeedbackWidget() {
  const t = useT();
  const user = useCurrentUser();
  const [open, setOpen] = useState(false);
  const [message, setMessage] = useState("");
  const [shot, setShot] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [toast, setToast] = useState<string | null>(null);

  const capture = useCallback(async () => {
    try {
      const canvas = await html2canvas(document.body, { logging: false, useCORS: true });
      setShot(canvas.toDataURL("image/png"));
    } catch {
      setShot(null);
    }
  }, []);

  const openPanel = useCallback(async () => {
    await capture(); // 先截图（面板未渲染），再打开
    setOpen(true);
  }, [capture]);

  const submit = useCallback(async () => {
    if (!message.trim()) return;
    setSending(true);
    // 仅工单详情页（?detail=）附 URL，其它页面忽略
    const hasDetail = new URLSearchParams(window.location.search).has("detail");
    try {
      await submitSiteFeedback({
        message: message.trim(),
        page_url: hasDetail ? window.location.href : null,
        screenshot: shot,
        user_email: user?.email ?? null,
      });
      setToast(t("反馈已发送，谢谢！"));
      setMessage("");
      setOpen(false);
    } catch {
      setToast(t("反馈发送失败"));
    } finally {
      setSending(false);
      setTimeout(() => setToast(null), 3000);
    }
  }, [message, shot, user, t]);

  // Esc 关闭弹框
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  return (
    <>
      {!open && (
        <button
          onClick={openPanel}
          className="fixed bottom-6 right-6 z-50 rounded-full px-4 py-3 text-white shadow-lg text-sm font-medium transition-transform hover:scale-105"
          style={{ backgroundColor: GOLD }}
        >
          {t("反馈")}
        </button>
      )}

      {open && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center p-4"
          style={{ backgroundColor: "rgba(0,0,0,0.55)" }}
          onClick={() => setOpen(false)}
        >
          <div
            className="w-full max-w-xl rounded-2xl bg-j-base text-j-fg shadow-2xl p-6"
            style={{ border: "1px solid var(--j-border)" }}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="text-lg font-semibold mb-4" style={{ color: GOLD }}>
              {t("提交反馈")}
            </div>
            <textarea
              autoFocus
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              placeholder={t("描述你遇到的问题…")}
              rows={8}
              className="w-full rounded-lg bg-j-surface text-j-fg placeholder:text-j-muted p-3 text-sm outline-none resize-y min-h-40 focus:ring-2"
              style={{ border: "1px solid var(--j-border)" }}
            />
            {shot && (
              <div className="mt-3 text-xs text-j-muted">
                ✓ {t("已自动截取当前屏幕")}
                <img
                  src={shot}
                  alt="screenshot"
                  className="mt-1.5 max-h-40 w-full object-cover object-top rounded-lg"
                  style={{ border: "1px solid var(--j-border)" }}
                />
              </div>
            )}
            <div className="mt-5 flex justify-end gap-3">
              <button
                onClick={() => setOpen(false)}
                className="text-sm px-4 py-2 rounded-lg text-j-muted hover:bg-j-hover transition-colors"
              >
                {t("取消")}
              </button>
              <button
                onClick={submit}
                disabled={sending || !message.trim()}
                className="text-sm px-5 py-2 rounded-lg text-white font-medium disabled:opacity-50 transition-opacity"
                style={{ backgroundColor: GOLD }}
              >
                {t("提交")}
              </button>
            </div>
          </div>
        </div>
      )}

      {toast && (
        <div className="fixed bottom-24 right-6 z-50 rounded-md bg-j-surface text-j-fg text-sm px-4 py-2 shadow-lg" style={{ border: "1px solid var(--j-border)" }}>
          {toast}
        </div>
      )}
    </>
  );
}
