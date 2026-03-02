"use client";

import { useContext } from "react";
import { usePathname } from "next/navigation";
import { useT, useLang, LangToggleContext } from "@/lib/i18n";

const NAV_ITEMS = [
  {
    href: "/",
    label: "工单分析",
    icon: "M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2",
  },
  {
    href: "/tracking",
    label: "工单跟踪",
    icon: "M15 12a3 3 0 11-6 0 3 3 0 016 0z M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z",
  },
  {
    href: "/feedback",
    label: "提交反馈",
    icon: "M12 9v6m3-3H9m12 0a9 9 0 11-18 0 9 9 0 0118 0z",
  },
  {
    href: "/oncall",
    label: "值班管理",
    icon: "M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z",
  },
  {
    href: "/analytics",
    label: "数据看板",
    icon: "M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z",
  },
  {
    href: "/rules",
    label: "分析规则",
    icon: "M4 6h16M4 10h16M4 14h16M4 18h16",
  },
  {
    href: "/reports",
    label: "值班报告",
    icon: "M9 17v-2m3 2v-4m3 4v-6m2 10H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z",
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

  const isActive = (href: string) =>
    href === "/" ? pathname === "/" : pathname.startsWith(href);

  return (
    <aside
      className="flex w-[216px] flex-shrink-0 flex-col"
      style={{ background: "#F8F9FA", borderRight: "1px solid rgba(0,0,0,0.08)" }}
    >
      {/* Logo */}
      <div
        className="flex h-[52px] items-center gap-3 px-5"
        style={{ borderBottom: "1px solid rgba(0,0,0,0.06)" }}
      >
        <div
          className="flex h-7 w-7 items-center justify-center rounded-lg"
          style={{ background: "#B8922E" }}
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
            <circle cx="10.5" cy="10.5" r="6" stroke="#FFFFFF" strokeWidth="2.5" />
            <path d="M15 15L20.5 20.5" stroke="#FFFFFF" strokeWidth="2.5" strokeLinecap="round" />
            <path d="M10.5 7V8.5M10.5 12.5V14M8 10.5H6.5M14.5 10.5H13" stroke="#FFFFFF" strokeWidth="1.5" strokeLinecap="round" />
            <circle cx="10.5" cy="10.5" r="1.2" fill="#FFFFFF" />
          </svg>
        </div>
        <div>
          <span
            className="text-sm font-semibold tracking-tight"
            style={{ color: "#111827" }}
          >
            Appllo
          </span>
          <span
            className="ml-1.5 rounded text-[9px] font-medium px-1 py-0.5"
            style={{ background: "rgba(184,146,46,0.10)", color: "#B8922E" }}
          >
            AI
          </span>
        </div>
      </div>

      {/* Nav */}
      <nav className="flex-1 space-y-px px-2.5 py-3">
        {NAV_ITEMS.map((item) => {
          const active = isActive(item.href);
          return (
            <a
              key={item.href}
              href={item.href}
              className="flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium transition-all duration-150"
              style={{
                color: active ? "#111827" : "#6B7280",
                background: active ? "rgba(184,146,46,0.08)" : "transparent",
                borderLeft: active ? "2px solid #B8922E" : "2px solid transparent",
              }}
              onMouseEnter={(e) => {
                if (!active) {
                  (e.currentTarget as HTMLElement).style.color = "#374151";
                  (e.currentTarget as HTMLElement).style.background =
                    "rgba(0,0,0,0.03)";
                }
              }}
              onMouseLeave={(e) => {
                if (!active) {
                  (e.currentTarget as HTMLElement).style.color = "#6B7280";
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
                <span
                  className="h-1.5 w-1.5 rounded-full flex-shrink-0"
                  style={{ background: "#B8922E" }}
                />
              )}
            </a>
          );
        })}
      </nav>

      {/* Footer */}
      <div
        className="px-2.5 py-3 space-y-px"
        style={{ borderTop: "1px solid rgba(0,0,0,0.06)" }}
      >
        {/* System status */}
        <a
          href="/settings"
          className="flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors"
          style={{ color: "#6B7280" }}
          onMouseEnter={(e) => {
            (e.currentTarget as HTMLElement).style.color = "#374151";
            (e.currentTarget as HTMLElement).style.background = "rgba(0,0,0,0.03)";
          }}
          onMouseLeave={(e) => {
            (e.currentTarget as HTMLElement).style.color = "#6B7280";
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
        </a>

        {/* Language toggle */}
        <button
          onClick={toggleLang}
          className="flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors"
          style={{ color: "#6B7280" }}
          onMouseEnter={(e) => {
            (e.currentTarget as HTMLElement).style.color = "#374151";
            (e.currentTarget as HTMLElement).style.background = "rgba(0,0,0,0.03)";
          }}
          onMouseLeave={(e) => {
            (e.currentTarget as HTMLElement).style.color = "#6B7280";
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
              color: "#6B7280",
            }}
          >
            {lang === "cn" ? "CN" : "EN"}
          </span>
        </button>
      </div>
    </aside>
  );
}
