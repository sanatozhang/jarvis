"use client";

import { useSearchParams } from "next/navigation";
import { Suspense } from "react";

const ERROR_MESSAGES: Record<string, string> = {
  domain_not_allowed: "请使用 @plaud.ai 邮箱登录",
  invalid_state:      "登录会话已过期，请重新登录",
  oauth_failed:       "Google 登录失败，请重试",
  google_unavailable: "Google 服务暂时不可用，请稍后重试",
};

function LoginContent() {
  const sp = useSearchParams();
  const error = sp.get("error");
  const next = sp.get("next") || "/";
  const message = error ? ERROR_MESSAGES[error] || "登录失败" : null;
  const loginHref = `/api/auth/google/login?next=${encodeURIComponent(next)}`;

  return (
    <div className="flex items-center justify-center min-h-screen bg-j-base text-j-fg">
      <div className="w-full max-w-sm rounded-2xl border border-j-fg/10 p-8 text-center shadow-sm">
        <h1 className="text-2xl font-semibold mb-1">Apollo</h1>
        <p className="text-sm text-j-fg/60 mb-6">Jarvis Ticket Platform</p>

        <a
          href={loginHref}
          className="inline-flex w-full items-center justify-center gap-2 rounded-lg
                     border border-j-fg/15 px-4 py-2 text-sm hover:bg-j-fg/5"
        >
          <span className="font-medium">G</span>
          <span>Sign in with Google</span>
        </a>

        <p className="mt-4 text-xs text-j-fg/50">仅限 @plaud.ai 邮箱</p>

        {message && (
          <p className="mt-4 text-sm text-red-500">{message}</p>
        )}
      </div>
    </div>
  );
}

export default function LoginPage() {
  return (
    <Suspense fallback={<div className="flex items-center justify-center min-h-screen text-j-fg/60">Loading…</div>}>
      <LoginContent />
    </Suspense>
  );
}
