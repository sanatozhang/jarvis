"use client";

import { useState, useEffect } from "react";
import { LangContext, type Lang } from "@/lib/i18n";

export default function LangProvider({ children }: { children: React.ReactNode }) {
  const [lang, setLang] = useState<Lang>("cn");

  useEffect(() => {
    const saved = localStorage.getItem("jarvis_lang");
    if (saved === "en" || saved === "cn") setLang(saved);
  }, []);

  const toggle = () => {
    const next = lang === "cn" ? "en" : "cn";
    setLang(next);
    localStorage.setItem("jarvis_lang", next);
  };

  return (
    <LangContext.Provider value={lang}>
      <div className="fixed top-3 right-4 z-50">
        <button
          onClick={toggle}
          className="flex items-center gap-1 rounded-full bg-white px-3 py-1.5 text-xs font-medium text-gray-600 shadow-md ring-1 ring-gray-200 transition-colors hover:bg-gray-50"
          title="Switch language"
        >
          <span className="text-sm">{lang === "cn" ? "🇨🇳" : "🇺🇸"}</span>
          {lang === "cn" ? "中文" : "EN"}
        </button>
      </div>
      {children}
    </LangContext.Provider>
  );
}
