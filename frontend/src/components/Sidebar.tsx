"use client";

import { useContext } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useT, useLang, LangToggleContext } from "@/lib/i18n";
import { useCurrentUser } from "@/components/AuthProvider";
import { ThemeToggle } from "@/components/ThemeToggle";

const NAV_ITEMS = [
  {
    href: "/crashguard",
    label: "崩溃看板",
    icon: "M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z",
  },
  {
    href: "/release",
    label: "发布管理",
    icon: "M12 19l9 2-9-18-9 18 9-2zm0 0v-8",
  },
  {
    href: "/settings",
    label: "系统设置",
    icon: "M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z M15 12a3 3 0 11-6 0 3 3 0 016 0z",
  },
];

export default function Sidebar() {
  const t = useT();
  const lang = useLang();
  const toggleLang = useContext(LangToggleContext);
  const pathname = usePathname();
  const me = useCurrentUser();

  const isActive = (href: string) =>
    href === "/" ? pathname === "/" : pathname.startsWith(href);

  return (
    <aside
      className="flex w-[216px] flex-shrink-0 flex-col"
      style={{ background: "var(--j-surface)", borderRight: "1px solid var(--j-border)" }}
    >
      {/* Brand — instrument plate + signal baseline signature */}
      <div style={{ borderBottom: "1px solid var(--j-border)" }}>
        <div className="flex items-center gap-2.5 px-5 pt-4 pb-3">
          <div
            className="flex h-8 w-8 items-center justify-center rounded-[8px]"
            style={{ background: "var(--j-accent)", boxShadow: "0 1px 2px rgba(14,124,134,0.35)" }}
          >
            {/* Instrument mark — a scope reading a signal */}
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
              <path d="M3 12h3l2.5-6 4 13 2.5-7h6" stroke="#FFFFFF" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </div>
          <div className="leading-none">
            <div className="flex items-center gap-1.5">
              <span className="font-display text-[15px] font-bold tracking-tight" style={{ color: "var(--j-ink)" }}>
                jarvis
              </span>
              <span
                className="rounded-[4px] font-mono text-[9px] font-medium px-1 py-0.5 tracking-wider"
                style={{ background: "var(--j-accent-soft)", color: "var(--j-accent)" }}
              >
                AI
              </span>
            </div>
            <div className="mt-1 font-mono text-[9px] uppercase tracking-[0.18em]" style={{ color: "var(--j-faint)" }}>
              Crash automation
            </div>
          </div>
        </div>
        {/* Signature: live oscilloscope baseline */}
        <div className="px-5 pb-3">
          <div className="j-signal-line" />
        </div>
      </div>

      {/* Nav */}
      <nav className="flex-1 space-y-px px-2.5 py-3">
        <div className="px-3 pb-2 font-mono text-[9px] uppercase tracking-[0.18em]" style={{ color: "var(--j-faint)" }}>
          // Console
        </div>
        {NAV_ITEMS.map((item) => {
          const active = isActive(item.href);
          return (
            <Link
              key={item.href}
              href={item.href}
              className="flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium transition-all duration-150"
              style={{
                color: active ? "var(--j-ink)" : "var(--j-graphite)",
                background: active ? "var(--j-accent-soft)" : "transparent",
                borderLeft: active ? "2px solid var(--j-accent)" : "2px solid transparent",
              }}
              onMouseEnter={(e) => {
                if (!active) {
                  (e.currentTarget as HTMLElement).style.color = "var(--j-ink)";
                  (e.currentTarget as HTMLElement).style.background =
                    "rgba(0,0,0,0.03)";
                }
              }}
              onMouseLeave={(e) => {
                if (!active) {
                  (e.currentTarget as HTMLElement).style.color = "var(--j-graphite)";
                  (e.currentTarget as HTMLElement).style.background = "transparent";
                }
              }}
            >
              <svg
                className="h-4 w-4 flex-shrink-0"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={active ? 2 : 1.5}
                suppressHydrationWarning
              >
                <path strokeLinecap="round" strokeLinejoin="round" d={item.icon} />
              </svg>
              <span className="flex-1 truncate">{t(item.label)}</span>
              {active && (
                /* live level-meter — bars breathe like a VU meter reading signal */
                <span className="j-meter flex items-end gap-[2px] flex-shrink-0" style={{ height: 10 }}>
                  <i style={{ display: "block", width: 2, height: 4, background: "var(--j-accent)", borderRadius: 1 }} />
                  <i style={{ display: "block", width: 2, height: 9, background: "var(--j-accent)", borderRadius: 1 }} />
                  <i style={{ display: "block", width: 2, height: 6, background: "var(--j-accent)", borderRadius: 1 }} />
                </span>
              )}
            </Link>
          );
        })}
      </nav>

      {/* Footer */}
      <div
        className="px-2.5 py-3 space-y-px"
        style={{ borderTop: "1px solid var(--j-border)" }}
      >
        {/* Current user + logout */}
        {me && (
          <div className="mb-2 px-3 pb-2" style={{ borderBottom: "1px solid var(--j-border)" }}>
            <div className="text-sm font-medium truncate" style={{ color: "var(--j-ink)" }}>
              {me.username}
            </div>
            <div className="text-xs truncate" style={{ color: "var(--j-graphite)" }}>
              {me.email || me.feishu_email}
            </div>
            <button
              onClick={async () => {
                try {
                  await fetch("/api/auth/logout", { method: "POST", credentials: "include" });
                } catch {}
                window.location.href = "/login";
              }}
              className="mt-2 flex items-center gap-2 rounded-lg px-2 py-1.5 text-sm font-medium transition-colors"
              style={{ color: "var(--j-graphite)" }}
              onMouseEnter={(e) => {
                (e.currentTarget as HTMLElement).style.color = "var(--j-ink)";
                (e.currentTarget as HTMLElement).style.background = "rgba(0,0,0,0.03)";
              }}
              onMouseLeave={(e) => {
                (e.currentTarget as HTMLElement).style.color = "var(--j-graphite)";
                (e.currentTarget as HTMLElement).style.background = "transparent";
              }}
            >
              <svg
                className="h-4 w-4 flex-shrink-0"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={1.5}
              >
                <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 9V5.25A2.25 2.25 0 0013.5 3h-6a2.25 2.25 0 00-2.25 2.25v13.5A2.25 2.25 0 007.5 21h6a2.25 2.25 0 002.25-2.25V15M12 9l-3 3m0 0l3 3m-3-3h12.75" />
              </svg>
              <span>{t("登出")}</span>
            </button>
          </div>
        )}

        {/* System status */}
        <Link
          href="/settings"
          className="flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors"
          style={{ color: "var(--j-graphite)" }}
          onMouseEnter={(e) => {
            (e.currentTarget as HTMLElement).style.color = "var(--j-ink)";
            (e.currentTarget as HTMLElement).style.background = "rgba(0,0,0,0.03)";
          }}
          onMouseLeave={(e) => {
            (e.currentTarget as HTMLElement).style.color = "var(--j-graphite)";
            (e.currentTarget as HTMLElement).style.background = "transparent";
          }}
        >
          <svg
            className="h-4 w-4 flex-shrink-0"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={1.5}
            suppressHydrationWarning
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M13 10V3L4 14h7v7l9-11h-7z" />
          </svg>
          <span className="flex-1">{t("系统状态")}</span>
          <span
            className="h-2 w-2 rounded-full"
            style={{ background: "#16A34A", boxShadow: "0 0 6px rgba(22,163,74,0.4)" }}
          />
        </Link>

        {/* Theme toggle — light / dark console mode */}
        <ThemeToggle />

        {/* Language toggle */}
        <button
          onClick={toggleLang}
          className="flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors"
          style={{ color: "var(--j-graphite)" }}
          onMouseEnter={(e) => {
            (e.currentTarget as HTMLElement).style.color = "var(--j-ink)";
            (e.currentTarget as HTMLElement).style.background = "rgba(0,0,0,0.03)";
          }}
          onMouseLeave={(e) => {
            (e.currentTarget as HTMLElement).style.color = "var(--j-graphite)";
            (e.currentTarget as HTMLElement).style.background = "transparent";
          }}
        >
          <svg
            className="h-4 w-4 flex-shrink-0"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={1.5}
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M10.5 21l5.25-11.25L21 21m-9-3h7.5M3 5.621a48.474 48.474 0 016-.371m0 0c1.12 0 2.233.038 3.334.114M9 5.25V3m3.334 2.364C11.176 10.658 7.69 15.08 3 17.502m9.334-12.138c.896.061 1.785.147 2.666.257m-4.589 8.495a18.023 18.023 0 01-3.827-5.802"
            />
          </svg>
          <span className="flex-1">{lang === "cn" ? "English" : "中文"}</span>
          <span
            className="rounded px-1.5 py-0.5 text-[10px] font-semibold"
            style={{
              background: "rgba(0,0,0,0.05)",
              color: "var(--j-graphite)",
            }}
          >
            {lang === "cn" ? "CN" : "EN"}
          </span>
        </button>
      </div>

    </aside>
  );
}
