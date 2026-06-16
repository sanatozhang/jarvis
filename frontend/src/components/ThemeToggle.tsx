"use client";

import { useEffect, useState } from "react";
import { useT } from "@/lib/i18n";

/**
 * Light / dark "console mode" toggle. Persists to localStorage; the pre-paint
 * script in layout.tsx applies the saved choice before first render (no FOUC).
 */
export function ThemeToggle() {
  const t = useT();
  const [dark, setDark] = useState(false);

  useEffect(() => {
    setDark(document.documentElement.classList.contains("dark"));
  }, []);

  const toggle = () => {
    const el = document.documentElement;
    el.classList.add("theme-anim");
    const next = !el.classList.contains("dark");
    el.classList.toggle("dark", next);
    try {
      localStorage.setItem("apollo_theme", next ? "dark" : "light");
    } catch {}
    setDark(next);
    window.setTimeout(() => el.classList.remove("theme-anim"), 340);
  };

  return (
    <button
      onClick={toggle}
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
      <svg className="h-4 w-4 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5} suppressHydrationWarning>
        {dark ? (
          <path strokeLinecap="round" strokeLinejoin="round" d="M12 3v2.25m6.364.386l-1.591 1.591M21 12h-2.25m-.386 6.364l-1.591-1.591M12 18.75V21m-4.773-4.227l-1.591 1.591M5.25 12H3m4.227-4.773L5.636 5.636M15.75 12a3.75 3.75 0 11-7.5 0 3.75 3.75 0 017.5 0z" />
        ) : (
          <path strokeLinecap="round" strokeLinejoin="round" d="M21.752 15.002A9.718 9.718 0 0118 15.75c-5.385 0-9.75-4.365-9.75-9.75 0-1.33.266-2.597.748-3.752A9.753 9.753 0 003 11.25C3 16.635 7.365 21 12.75 21a9.753 9.753 0 009.002-5.998z" />
        )}
      </svg>
      <span className="flex-1 text-left">{dark ? t("亮色模式") : t("暗色模式")}</span>
      <span
        className="rounded px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-wider"
        style={{ background: "var(--j-accent-soft)", color: "var(--j-accent)" }}
      >
        {dark ? "DARK" : "LIGHT"}
      </span>
    </button>
  );
}
