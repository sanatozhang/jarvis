"use client";

import { useState, useEffect } from "react";
import { LangContext, LangToggleContext, type Lang } from "@/lib/i18n";

// 默认 English；切换后持久化到 localStorage，刷新/切 tab 都保留选择。
export default function LangProvider({ children }: { children: React.ReactNode }) {
  const [lang, setLang] = useState<Lang>("en");

  useEffect(() => {
    try {
      const saved = localStorage.getItem("appllo_lang");
      if (saved === "en" || saved === "cn") setLang(saved);
    } catch {}
  }, []);

  const toggle = () => {
    setLang((prev) => {
      const next = prev === "cn" ? "en" : "cn";
      try {
        localStorage.setItem("appllo_lang", next);
      } catch {}
      return next;
    });
  };

  return (
    <LangContext.Provider value={lang}>
      <LangToggleContext.Provider value={toggle}>
        {children}
      </LangToggleContext.Provider>
    </LangContext.Provider>
  );
}
