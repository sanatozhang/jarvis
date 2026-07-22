"use client";

import { useRouter, usePathname } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { useAuth } from "./AuthProvider";
import { useT } from "@/lib/i18n";

const S = {
  surface: "var(--j-surface)", overlay: "var(--j-panel)",
  border: "var(--j-border)", accent: "var(--j-accent)",
  text1: "var(--j-ink)", text2: "var(--j-graphite)",
};

export function AuthGate({ children }: { children: ReactNode }) {
  const { state, ssoActive, registerLegacyUser } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const t = useT();

  useEffect(() => {
    if (!ssoActive) return;
    if (state.status !== "anonymous") return;
    if (pathname === "/login") return;
    const next = encodeURIComponent(pathname || "/");
    router.replace(`/login?next=${next}`);
  }, [state.status, ssoActive, pathname, router]);

  // 非 SSO 部署下，任何页面（不只是首页）第一次遇到没有本地用户名的访客都要拦截注册——
  // 之前这个弹窗只挂在 page.tsx（首页），从 /feedback 等其他页面直接进来的新用户会绕过
  // 注册，导致提交的工单 created_by 落库为空字符串（看起来像"没有提交者"）。
  const [usernameInput, setUsernameInput] = useState("");
  const [usernameTouched, setUsernameTouched] = useState(false);
  const [emailInput, setEmailInput] = useState("");
  const [setupError, setSetupError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const showUsernameSetup = !ssoActive && state.status === "anonymous";

  const onEmailChange = (v: string) => {
    setEmailInput(v);
    // 用户名默认取邮箱前缀，减少新用户输入负担；一旦手动改过用户名就不再自动覆盖
    if (!usernameTouched) setUsernameInput(v.split("@")[0] || "");
  };

  const submitSetup = async () => {
    const name = usernameInput.trim();
    const email = emailInput.trim();
    if (!name || !email) return;
    if (!/^[a-zA-Z0-9._%+-]+@plaud\.ai$/.test(email)) {
      setSetupError(t("邮箱必须以 @plaud.ai 结尾"));
      return;
    }
    setSubmitting(true);
    try {
      await registerLegacyUser(name, email);
      setSetupError("");
    } catch (err: any) {
      const msg = err?.message || String(err);
      setSetupError(msg.includes("plaud.ai") ? t("邮箱必须以 @plaud.ai 结尾") : msg);
    } finally {
      setSubmitting(false);
    }
  };

  if (state.status === "loading") {
    return <div className="flex items-center justify-center h-screen text-j-fg/60">Loading…</div>;
  }
  if (ssoActive && state.status === "anonymous" && pathname !== "/login") {
    return null;
  }

  return (
    <>
      {children}
      {showUsernameSetup && (
        <div className="fixed inset-0 z-[70] flex items-center justify-center" style={{ background: "rgba(0,0,0,0.75)" }}>
          <div className="w-full max-w-sm rounded-2xl p-6" style={{ background: S.surface, border: `1px solid ${S.border}` }}>
            <div className="mb-5 text-center">
              <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-full"
                style={{ background: S.accent }}>
                <svg width="22" height="22" viewBox="0 0 24 24" fill="none">
                  <circle cx="10.5" cy="10.5" r="6" stroke="#0A0B0E" strokeWidth="2.5" />
                  <path d="M15 15L20.5 20.5" stroke="#0A0B0E" strokeWidth="2.5" strokeLinecap="round" />
                  <path d="M10.5 7V8.5M10.5 12.5V14M8 10.5H6.5M14.5 10.5H13" stroke="#0A0B0E" strokeWidth="1.5" strokeLinecap="round" />
                  <circle cx="10.5" cy="10.5" r="1.2" fill="#0A0B0E" />
                </svg>
              </div>
              <h3 className="text-base font-semibold" style={{ color: S.text1 }}>{t("欢迎使用 Apollo")}</h3>
              <p className="mt-1 text-sm" style={{ color: S.text2 }}>{t("请使用 @plaud.ai 邮箱注册")}</p>
            </div>
            <input
              type="email"
              autoFocus
              value={emailInput}
              onChange={(e) => onEmailChange(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && usernameInput.trim() && emailInput.trim()) submitSetup(); }}
              placeholder={t("邮箱（@plaud.ai）")}
              className="mb-3 w-full rounded-lg px-4 py-2.5 text-center text-sm outline-none font-sans"
              style={{ background: S.overlay, border: `1px solid ${S.border}`, color: S.text1 }}
            />
            <input
              value={usernameInput}
              onChange={(e) => { setUsernameInput(e.target.value); setUsernameTouched(true); }}
              onKeyDown={(e) => { if (e.key === "Enter" && usernameInput.trim() && emailInput.trim()) submitSetup(); }}
              placeholder={t("用户名")}
              className="mb-2 w-full rounded-lg px-4 py-2.5 text-center text-sm outline-none font-sans"
              style={{ background: S.overlay, border: `1px solid ${S.border}`, color: S.text1 }}
            />
            {setupError && (
              <p className="mb-3 text-center text-xs" style={{ color: "#DC2626" }}>{setupError}</p>
            )}
            <button
              onClick={submitSetup}
              disabled={!usernameInput.trim() || !emailInput.trim() || submitting}
              className="w-full rounded-lg py-2.5 text-sm font-semibold transition-colors disabled:opacity-30"
              style={{ background: S.accent, color: "#FFFFFF" }}>
              {t("开始使用")}
            </button>
          </div>
        </div>
      )}
    </>
  );
}
