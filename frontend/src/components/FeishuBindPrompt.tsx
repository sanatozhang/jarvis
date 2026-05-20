"use client";

import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useAuth, useCurrentUser } from "@/components/AuthProvider";
import {
  shouldShowBindPrompt,
  markBindPromptedToday,
  persistBoundEmail,
} from "@/lib/auth";

const BIND_SUCCESS_MSG = "✓ 飞书邮箱绑定成功";
const BIND_ERROR_MSG: Record<string, string> = {
  invalid_state: "登录会话已过期，请重试",
  oauth_failed: "飞书登录失败，请重试",
  domain_not_allowed: "请使用 @plaud.ai 邮箱",
  user_not_found: "未找到当前用户",
  missing_username: "缺少用户名",
  no_email: "未获取到邮箱信息",
};

export function FeishuBindPrompt() {
  const { state, ssoActive } = useAuth();
  const user = useCurrentUser();
  const sp = useSearchParams();
  const [open, setOpen] = useState(false);
  const [flash, setFlash] = useState<string | null>(null);
  const [flashKind, setFlashKind] = useState<"ok" | "error">("ok");

  // Handle ?feishu_bind=ok|error query after callback redirect
  useEffect(() => {
    const bindResult = sp.get("feishu_bind");
    if (!bindResult) return;
    if (bindResult === "ok") {
      const email = sp.get("email") || "";
      if (email) persistBoundEmail(email);
      setFlash(BIND_SUCCESS_MSG);
      setFlashKind("ok");
      // remove query without reload
      const url = new URL(window.location.href);
      url.searchParams.delete("feishu_bind");
      url.searchParams.delete("email");
      window.history.replaceState({}, "", url.toString());
    } else if (bindResult === "error") {
      const reason = sp.get("reason") || "";
      setFlash(BIND_ERROR_MSG[reason] || "飞书绑定失败");
      setFlashKind("error");
      const url = new URL(window.location.href);
      url.searchParams.delete("feishu_bind");
      url.searchParams.delete("reason");
      window.history.replaceState({}, "", url.toString());
    }
    const t = setTimeout(() => setFlash(null), 5000);
    return () => clearTimeout(t);
  }, [sp]);

  // Decide whether to show the prompt
  useEffect(() => {
    if (state.status !== "authed") return;
    setOpen(shouldShowBindPrompt(user, ssoActive));
  }, [state.status, user, ssoActive]);

  const handleBind = () => {
    if (!user) return;
    markBindPromptedToday();
    const next = window.location.pathname + window.location.search;
    window.location.href =
      `/api/auth/feishu/bind-login?username=${encodeURIComponent(user.username)}` +
      `&next=${encodeURIComponent(next)}`;
  };

  const handleDismiss = () => {
    markBindPromptedToday();
    setOpen(false);
  };

  return (
    <>
      {flash && (
        <div
          className="fixed left-1/2 top-1/2 z-50 rounded-xl px-8 py-5 text-lg font-medium shadow-2xl"
          style={{
            transform: "translate(-50%, -50%)",
            background: flashKind === "ok" ? "#16A34A" : "#DC2626",
            color: "white",
            minWidth: "280px",
            textAlign: "center",
          }}
        >
          {flash}
        </div>
      )}
      {open && user && (
        <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/30">
          <div
            className="w-full max-w-sm rounded-2xl bg-white p-6 shadow-xl"
            style={{ color: "#111827" }}
          >
            <h2 className="text-lg font-semibold mb-2">绑定飞书邮箱</h2>
            <p className="text-sm text-j-fg/70 mb-5">
              为了接收飞书通知，请用飞书账号一键绑定您的邮箱（@plaud.ai）。
              <br />
              <span className="text-xs text-j-fg/50">当前账号：{user.username}</span>
            </p>
            <div className="flex justify-end gap-2">
              <button
                onClick={handleDismiss}
                className="rounded-lg px-3 py-1.5 text-sm hover:bg-black/5"
                style={{ color: "#6B7280" }}
              >
                稍后
              </button>
              <button
                onClick={handleBind}
                className="rounded-lg px-3 py-1.5 text-sm font-medium"
                style={{ background: "#B8922E", color: "white" }}
              >
                去飞书绑定
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
