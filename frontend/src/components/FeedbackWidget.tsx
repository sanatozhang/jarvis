"use client";

import { useState, useCallback } from "react";
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

  return (
    <>
      {!open && (
        <button
          onClick={openPanel}
          className="fixed bottom-6 right-6 z-50 rounded-full px-4 py-3 text-white shadow-lg text-sm font-medium"
          style={{ backgroundColor: GOLD }}
        >
          {t("反馈")}
        </button>
      )}

      {open && (
        <div className="fixed bottom-6 right-6 z-50 w-80 rounded-xl bg-white dark:bg-neutral-900 shadow-2xl border border-neutral-200 dark:border-neutral-700 p-4">
          <div className="text-sm font-semibold mb-2" style={{ color: GOLD }}>{t("提交反馈")}</div>
          <textarea
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            placeholder={t("描述你遇到的问题…")}
            rows={4}
            className="w-full rounded-md border border-neutral-300 dark:border-neutral-600 bg-transparent p-2 text-sm outline-none"
          />
          {shot && (
            <div className="mt-2 text-xs text-neutral-500">
              ✓ {t("已自动截取当前屏幕")}
              <img src={shot} alt="screenshot" className="mt-1 max-h-24 w-full object-cover rounded border border-neutral-200 dark:border-neutral-700" />
            </div>
          )}
          <div className="mt-3 flex justify-end gap-2">
            <button onClick={() => setOpen(false)} className="text-sm px-3 py-1.5 rounded-md text-neutral-500">{t("取消")}</button>
            <button
              onClick={submit}
              disabled={sending || !message.trim()}
              className="text-sm px-3 py-1.5 rounded-md text-white disabled:opacity-50"
              style={{ backgroundColor: GOLD }}
            >
              {t("提交")}
            </button>
          </div>
        </div>
      )}

      {toast && (
        <div className="fixed bottom-24 right-6 z-50 rounded-md bg-neutral-900 text-white text-sm px-4 py-2 shadow-lg">
          {toast}
        </div>
      )}
    </>
  );
}
